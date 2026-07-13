from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from kestrel_sdk.voice import (
    ConversationCapabilities,
    ConversationEvent,
    ConversationProvider,
    ConversationSession,
    EphemeralClientSecret,
    RealtimeClientSession,
    RealtimeTransport,
    ToolCallBatchRequestedEvent,
    TTSProvider,
    VoiceConfig,
    VoiceToolCall,
    VoiceToolResult,
    TranscriptDeltaEvent,
)


class _TTS(TTSProvider):
    name = "legacy"
    is_local = False

    async def synthesize(self, *args: Any, **kwargs: Any) -> bytes:
        return b""

    async def synthesize_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[bytes]:
        if False:
            yield b""

    async def list_voices(self) -> list:
        return []

    async def is_available(self) -> bool:
        return True


class _FormatlessTTS(_TTS):
    def supported_output_formats(self) -> tuple[str, ...]:
        return ()


class _Session(ConversationSession):
    session_id = "session"

    def __init__(self) -> None:
        self.committed: list[tuple[str, Any]] = []

    async def send_audio(self, pcm_chunk: bytes) -> None:
        return None

    async def receive(self) -> AsyncIterator[ConversationEvent]:
        if False:
            yield  # pragma: no cover

    async def commit_tool_result(self, call_id: str, result: Any) -> None:
        self.committed.append((call_id, result))

    async def update_instructions(self, instructions: str) -> None:
        return None

    async def cancel_response(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _BatchSession(_Session):
    def __init__(self) -> None:
        super().__init__()
        self.batches = 0

    async def commit_tool_results(self, results: list[VoiceToolResult]) -> None:
        self.batches += 1
        self.committed.extend((item.call_id, item.result) for item in results)


class _Provider(ConversationProvider):
    name = "example_realtime"
    is_local = False

    async def create_session(self, **kwargs: Any) -> ConversationSession:
        return _Session()

    async def discover_models(self) -> list[str]:
        return []

    async def list_voices(self) -> list:
        return []

    async def is_available(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_single_tool_providers_remain_batch_compatible() -> None:
    session = _Session()
    await session.commit_tool_results(
        [VoiceToolResult("one", {"x": 1}), VoiceToolResult("two", {"x": 2})]
    )
    assert session.committed == [("one", {"x": 1}), ("two", {"x": 2})]


@pytest.mark.asyncio
async def test_parallel_provider_can_commit_one_batch() -> None:
    session = _BatchSession()
    await session.commit_tool_results(
        [VoiceToolResult("one", 1), VoiceToolResult("two", 2)]
    )
    assert session.batches == 1
    assert session.committed == [("one", 1), ("two", 2)]


def test_conversation_capabilities_serialize_specialized_tools() -> None:
    caps = ConversationCapabilities(
        vendor="xai",
        transports=(RealtimeTransport.WEBSOCKET,),
        supports_parallel_tool_calls=True,
        supports_ephemeral_auth=True,
        server_tools=("web_search", "x_search", "file_search", "mcp"),
    )
    assert caps.to_dict()["transports"] == ["websocket"]
    assert caps.to_dict()["server_tools"] == [
        "web_search",
        "x_search",
        "file_search",
        "mcp",
    ]


def test_legacy_provider_gets_conservative_capabilities() -> None:
    caps = _Provider().provider_capabilities()
    assert caps.vendor == "example"
    assert caps.transports == ()
    assert caps.supports_parallel_tool_calls is False


def test_provider_scoped_voices_do_not_leak_between_catalogs() -> None:
    config = VoiceConfig(tts_provider="openai", tts_voice_id="cedar")
    assert config.voice_for("openai") == "cedar"
    assert config.voice_for("openai_realtime") == "cedar"
    assert config.voice_for("xai_realtime") == ""

    config.set_voice_for("xai_realtime", "eve")
    config.set_voice_for("openai_realtime", "marin")
    assert config.voice_for("xai_realtime") == "eve"
    assert config.voice_for("openai_realtime") == "marin"

    restored = VoiceConfig.from_dict(config.to_dict())
    assert restored.provider_voice_ids == {
        "xai_realtime": "eve",
        "openai_realtime": "marin",
    }


def test_provider_scoped_voice_keys_are_normalized() -> None:
    config = VoiceConfig(
        provider_voice_ids={" XAI_Realtime ": " custom-01 "},
        conversation_provider=" XAI_Realtime ",
    )
    assert config.voice_for("xai_realtime") == "custom-01"
    assert config.conversation_provider == "xai_realtime"


def test_transcript_updates_can_declare_cumulative_corrections() -> None:
    event = TranscriptDeltaEvent("hello world", is_cumulative=True)
    assert event.is_cumulative is True


def test_legacy_tts_provider_gets_format_capabilities() -> None:
    provider = _TTS()
    assert provider.supported_output_formats() == ("opus", "mp3", "pcm", "wav")
    assert provider.default_output_format() == "opus"
    assert _FormatlessTTS().default_output_format() == "opus"


@pytest.mark.asyncio
async def test_provider_without_browser_transport_rejects_client_mint() -> None:
    with pytest.raises(NotImplementedError, match="browser client sessions"):
        await _Provider().mint_client_session(
            voice="",
            instructions="",
            tools=[],
            turn_detection=None,  # type: ignore[arg-type]
            audio_format=None,  # type: ignore[arg-type]
        )


def test_legacy_openai_session_adapter_accepts_numeric_expiry() -> None:
    legacy = type(
        "LegacySession",
        (),
        {
            "session_id": "legacy-1",
            "model": "runtime-model",
            "voice": "cedar",
            "client_secret": "short-lived",
            "expires_at": "123.9",
        },
    )()
    session = RealtimeClientSession.from_legacy_openai("openai_realtime", legacy)
    assert session.client_secret.expires_at == 123
    assert session.provider == "openai_realtime"


def test_client_bootstrap_and_tool_batch_are_provider_neutral() -> None:
    session = RealtimeClientSession(
        session_id="s1",
        provider="xai_realtime",
        vendor="xai",
        transport=RealtimeTransport.WEBSOCKET,
        protocol="xai-realtime-v1",
        endpoint="wss://api.x.ai/v1/realtime",
        model="grok-voice-latest",
        voice="eve",
        client_secret=EphemeralClientSecret("secret", 123),
        session_config={"voice": "eve"},
    )
    event = ToolCallBatchRequestedEvent(
        calls=(VoiceToolCall("c1", "search", {"q": "kestrel"}),),
        batch_id="response-1",
    )
    assert session.transport is RealtimeTransport.WEBSOCKET
    assert event.calls[0].name == "search"
