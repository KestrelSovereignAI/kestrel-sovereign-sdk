"""
ConversationProvider — speech-to-speech provider contract.

Sibling to :class:`TTSProvider` and :class:`STTProvider`, but with a
fundamentally different shape. Speech-to-speech models (OpenAI Realtime and
the Kestrel-agnostic realtime APIs that follow) own the **full conversational
turn**: audio in → LLM reasoning → audio out, all within one session. They
can't be modeled as ``bytes ↔ text`` transforms because:

* The LLM lives inside the session, not on the caller's side.
* Tool calls happen mid-utterance and need a live round-trip.
* Interruption (barge-in) is a first-class control, not an emergent property
  of chunk scheduling.
* Turn detection (voice activity, semantic VAD) is configured on the session
  and emits events rather than being polled.

This module defines:

* :class:`ConversationProvider` — factory for sessions.
* :class:`ConversationSession` — the live session handle.
* The :class:`ConversationEvent` union — everything a session can emit.
* :class:`AudioFormat`, :class:`TurnDetectionConfig`, :class:`ToolDef` —
  supporting types with no Kestrel-specific coupling.

Providers implementing this ABC register via the entry-points group
``kestrel_sovereign.conversation_providers``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Literal, Optional, Union

from .base import VoiceInfo


# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioFormat:
    """Wire format for audio frames on a Realtime session.

    OpenAI Realtime accepts PCM16 at 24 kHz mono little-endian; other future
    providers may accept Opus or higher sample rates. Providers should accept
    the formats listed in :meth:`ConversationProvider.supported_audio_formats`
    (added when a second vendor joins); callers pick one per session.
    """

    sample_rate: int = 24000
    encoding: Literal["pcm16", "opus"] = "pcm16"
    channels: int = 1


@dataclass(frozen=True)
class TurnDetectionConfig:
    """How the session decides when the user is done speaking.

    ``mode`` values:

    * ``"server_vad"`` — provider-side voice activity detection; auto-commits
      the user's audio buffer on speech-stop.
    * ``"semantic_vad"`` — content-aware: waits for natural end-of-utterance
      cues in the partial transcript. Higher latency, fewer interruptions.
    * ``"none"`` — caller drives turn boundaries explicitly (rare; used when
      the frontend already has its own VAD).
    """

    mode: Literal["server_vad", "semantic_vad", "none"] = "server_vad"
    silence_ms: int = 500
    threshold: float = 0.5
    # When True, the session automatically starts producing a response when
    # the user's turn ends. Set False to let the agent explicitly trigger
    # responses (e.g. "the agent was interrupted; wait for the user to
    # prompt again").
    create_response: bool = True


@dataclass(frozen=True)
class ToolDef:
    """Tool exposed to the realtime session.

    Mirrors the shape of OpenAI's tool schema so providers can forward it
    verbatim. ``parameters_schema`` is a JSON Schema dict. We intentionally
    do NOT import the sovereign-side Tool/ToolCategory types here — the SDK
    stays dependency-free so third-party packages can register conversation
    providers without pulling in the whole agent runtime.
    """

    name: str
    description: str
    parameters_schema: dict


class RealtimeTransport(str, Enum):
    """Browser transport used by a realtime conversation provider."""

    WEBRTC = "webrtc"
    WEBSOCKET = "websocket"


@dataclass(frozen=True)
class ConversationCapabilities:
    """Provider-level speech-to-speech capability metadata.

    This is the voice analogue of ``llm.ProviderCapabilities``: routing and
    UI code can select providers by declared behavior instead of branching on
    provider names. Runtime model and voice discovery remain authoritative.
    """

    vendor: str
    transports: tuple[RealtimeTransport, ...] = ()
    supports_function_tools: bool = True
    supports_parallel_tool_calls: bool = False
    server_tools: tuple[str, ...] = ()
    supports_barge_in: bool = True
    supports_session_resumption: bool = False
    supports_ephemeral_auth: bool = False
    voices_are_dynamic: bool = True
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "transports": [transport.value for transport in self.transports],
            "supports_function_tools": self.supports_function_tools,
            "supports_parallel_tool_calls": self.supports_parallel_tool_calls,
            "server_tools": list(self.server_tools),
            "supports_barge_in": self.supports_barge_in,
            "supports_session_resumption": self.supports_session_resumption,
            "supports_ephemeral_auth": self.supports_ephemeral_auth,
            "voices_are_dynamic": self.voices_are_dynamic,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class EphemeralClientSecret:
    """Short-lived credential safe to return to an authenticated client."""

    value: str
    expires_at: int


@dataclass(frozen=True)
class RealtimeClientSession:
    """Provider-neutral bootstrap for a browser realtime client.

    ``session_config`` is empty when configuration is bound while minting and
    populated when the client must send ``session.update`` after connecting.
    It must never contain a long-lived API key.
    """

    session_id: str
    provider: str
    vendor: str
    transport: RealtimeTransport
    protocol: str
    endpoint: str
    model: str
    voice: str
    client_secret: EphemeralClientSecret
    session_config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_legacy_openai(cls, provider: str, session: Any) -> "RealtimeClientSession":
        """Adapt the pre-0.30 OpenAI ephemeral-session return shape."""
        return cls(
            session_id=str(session.session_id),
            provider=provider,
            vendor="openai",
            transport=RealtimeTransport.WEBRTC,
            protocol="openai-realtime-ga",
            endpoint="https://api.openai.com/v1/realtime/calls",
            model=str(session.model),
            voice=str(session.voice),
            client_secret=EphemeralClientSecret(
                value=str(session.client_secret),
                expires_at=int(float(session.expires_at)),
            ),
        )


@dataclass(frozen=True)
class VoiceToolCall:
    """One governed function call requested during a voice response."""

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceToolResult:
    """Result paired to a pending :class:`VoiceToolCall`."""

    call_id: str
    result: Any


# ---------------------------------------------------------------------------
# Event union
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionCreatedEvent:
    session_id: str
    kind: Literal["session.created"] = "session.created"


@dataclass(frozen=True)
class SessionUpdatedEvent:
    session_id: str
    kind: Literal["session.updated"] = "session.updated"


@dataclass(frozen=True)
class SpeechStartedEvent:
    """User started speaking. Callers should flush playback for barge-in."""

    kind: Literal["input_audio.speech_started"] = "input_audio.speech_started"


@dataclass(frozen=True)
class SpeechStoppedEvent:
    """User stopped speaking. The session will now process their utterance."""

    kind: Literal["input_audio.speech_stopped"] = "input_audio.speech_stopped"


@dataclass(frozen=True)
class TranscriptDeltaEvent:
    """Partial transcript of the user's in-progress utterance."""

    text: str
    is_final: bool = False
    # Some providers send the entire corrected transcript on each update;
    # consumers must replace rather than append when this is true.
    is_cumulative: bool = False
    kind: Literal["input_audio.transcript_delta"] = "input_audio.transcript_delta"


@dataclass(frozen=True)
class TranscriptFinalEvent:
    """Final (committed) transcript of the user's utterance."""

    text: str
    kind: Literal["input_audio.transcript_final"] = "input_audio.transcript_final"


@dataclass(frozen=True)
class ResponseAudioDeltaEvent:
    """One chunk of agent-spoken audio (in the session's configured format)."""

    pcm_chunk: bytes
    kind: Literal["response.audio_delta"] = "response.audio_delta"


@dataclass(frozen=True)
class ResponseTextDeltaEvent:
    """One chunk of the agent's spoken text (running transcript for UI)."""

    text: str
    kind: Literal["response.text_delta"] = "response.text_delta"


@dataclass(frozen=True)
class ResponseDoneEvent:
    """The agent finished this response turn."""

    kind: Literal["response.done"] = "response.done"


@dataclass(frozen=True)
class ToolCallRequestedEvent:
    """The agent wants to invoke a registered tool.

    The caller executes the tool and calls
    :meth:`ConversationSession.commit_tool_result` with the matching
    ``call_id``.
    """

    call_id: str
    name: str
    arguments: dict
    kind: Literal["response.tool_call_requested"] = "response.tool_call_requested"
    batch_id: str = ""


@dataclass(frozen=True)
class ToolCallBatchRequestedEvent:
    """All client-executed function calls belonging to one response."""

    calls: tuple[VoiceToolCall, ...]
    batch_id: str = ""
    kind: Literal["response.tool_call_batch_requested"] = (
        "response.tool_call_batch_requested"
    )


@dataclass(frozen=True)
class ErrorEvent:
    """Session-level error. May or may not be fatal; check ``code``."""

    message: str
    code: Optional[str] = None
    kind: Literal["error"] = "error"


ConversationEvent = Union[
    SessionCreatedEvent,
    SessionUpdatedEvent,
    SpeechStartedEvent,
    SpeechStoppedEvent,
    TranscriptDeltaEvent,
    TranscriptFinalEvent,
    ResponseAudioDeltaEvent,
    ResponseTextDeltaEvent,
    ResponseDoneEvent,
    ToolCallRequestedEvent,
    ToolCallBatchRequestedEvent,
    ErrorEvent,
]


# ---------------------------------------------------------------------------
# ABCs
# ---------------------------------------------------------------------------


class ConversationSession(ABC):
    """Live speech-to-speech session. One per voice conversation turn/call.

    Lifecycle:

    1. Caller obtains via :meth:`ConversationProvider.create_session`.
    2. Call :meth:`send_audio` with user PCM chunks.
    3. Consume :meth:`receive` (async iterator) for events.
    4. On :class:`ToolCallRequestedEvent`: execute the tool, then
       :meth:`commit_tool_result`.
    5. On barge-in from the UI: :meth:`cancel_response`.
    6. End with :meth:`close`.

    Implementations must be safe to call :meth:`close` multiple times.
    """

    session_id: str

    @abstractmethod
    async def send_audio(self, pcm_chunk: bytes) -> None:
        """Append one PCM chunk to the input buffer.

        The chunk must match the session's configured :class:`AudioFormat`.
        """

    @abstractmethod
    def receive(self) -> AsyncIterator[ConversationEvent]:
        """Stream events from the session. Exits when the session closes."""

    @abstractmethod
    async def commit_tool_result(self, call_id: str, result: Any) -> None:
        """Return the result of a tool the session requested.

        ``call_id`` matches :class:`ToolCallRequestedEvent.call_id`. ``result``
        will be serialized via ``json.dumps`` by the provider.
        """

    async def commit_tool_results(self, results: list[VoiceToolResult]) -> None:
        """Return every result in one model tool-call batch.

        The compatibility implementation calls :meth:`commit_tool_result` for
        each result. Providers that support parallel calls must override this
        method so they submit every output and trigger exactly one continuation.
        """
        for item in results:
            await self.commit_tool_result(item.call_id, item.result)

    @abstractmethod
    async def update_instructions(self, instructions: str) -> None:
        """Replace the session's ``instructions`` field mid-session.

        Used by the Realtime tag adapter (#724) to surface per-turn tag
        directives composed from ``[excited]``/``[whispering]``/etc.
        """

    @abstractmethod
    async def cancel_response(self) -> None:
        """Interrupt the in-flight agent response (barge-in).

        Implementations should stop emitting :class:`ResponseAudioDeltaEvent`
        as soon as possible; callers typically also flush their playback
        buffer.
        """

    @abstractmethod
    async def close(self) -> None:
        """Terminate the session. Idempotent."""


class ConversationProvider(ABC):
    """Factory for :class:`ConversationSession` instances.

    Registered via the ``kestrel_sovereign.conversation_providers``
    entry-points group. ``name`` matches the string the routing resolver
    (#723) stores in ``VoiceRoute.conversation_provider``.
    """

    name: str
    is_local: bool  # False for any cloud-backed provider; used by privacy gating.

    def provider_capabilities(self) -> ConversationCapabilities:
        """Describe the provider route without probing the network.

        The conservative default preserves third-party providers written
        before this contract existed. New providers should override it.
        """
        vendor = (self.name or "").split("_", 1)[0]
        return ConversationCapabilities(vendor=vendor)

    async def mint_client_session(
        self,
        *,
        voice: str,
        instructions: str,
        tools: list[ToolDef],
        turn_detection: TurnDetectionConfig,
        audio_format: AudioFormat,
    ) -> RealtimeClientSession:
        """Mint a browser-safe realtime bootstrap.

        Server-mediated-only providers may leave this unimplemented and omit
        browser transports from :meth:`provider_capabilities`.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support browser client sessions"
        )

    @abstractmethod
    async def create_session(
        self,
        *,
        voice: str,
        instructions: str,
        tools: list[ToolDef],
        turn_detection: TurnDetectionConfig,
        audio_format: AudioFormat,
    ) -> ConversationSession:
        """Open a new live session configured for this turn."""

    @abstractmethod
    async def discover_models(self) -> list[str]:
        """Runtime-discovered realtime-capable model IDs.

        Never hardcoded — providers call out to their vendor's ``/models``
        endpoint (filtered to realtime-capable) and cache. Empty list is
        allowed (provider discovered nothing; caller surfaces a useful
        error).
        """

    @abstractmethod
    async def list_voices(self) -> list[VoiceInfo]:
        """Runtime-discovered voices this provider exposes, with metadata.

        Returns full :class:`VoiceInfo` so the picker can render gender,
        accent, and energy without scanning sibling TTS providers. Each
        provider owns its catalog: if metadata isn't queryable from the
        upstream API, surface what's available (``name`` + ``voice_id`` at
        minimum) and leave other fields at the dataclass defaults. No
        hardcoded names — same rule as :meth:`discover_models`.
        """

    @abstractmethod
    async def is_available(self) -> bool:
        """True if the provider can currently open sessions.

        Typically checks for API key presence and a cheap reachability probe.
        """
