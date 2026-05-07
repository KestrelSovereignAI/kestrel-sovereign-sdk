"""LLM response envelope — what every adapter returns.

This is the wire shape the framework reads after every
``LLMAdapter.get_response`` call. Third-party provider plugins
construct one of these and the framework's routing, usage tracking,
and tool-dispatch layers all read the same fields, regardless of
which provider produced the call.

The shape is permissive on purpose: most providers do not report
prompt-cache usage (those fields are ``None``), and any single call
either yields ``content`` (text reply) or ``tool_calls`` (function
invocation) — rarely both, but both are allowed since some providers
emit a leading text segment alongside tool calls.

For migration context: this type was promoted from
``kestrel_sovereign.llm.adapter`` in SDK 0.5.0 alongside ``LLMAdapter``
itself, so third-party provider packages can depend only on
``kestrel-sovereign-sdk``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    """One function-call request emitted by the model.

    The shape mirrors OpenAI's tool-calling format because that is the
    union format the framework normalizes every provider into. Anthropic
    ``tool_use`` blocks and Gemini ``functionCall`` parts are translated
    into this shape inside their respective adapters before being
    returned in :class:`LLMResponse`.

    Attributes:
        id: Provider-supplied identifier the framework echoes back when
            it returns the tool result on the next turn. Format varies
            by provider (OpenAI ``call_*``, Anthropic ``toolu_*``);
            adapters do not coerce — they pass it through.
        name: The function name the model wants to invoke. Must match
            a tool registered in the agent's tool registry.
        arguments: Pre-parsed argument dict. Adapters parse the
            provider-native form (JSON string, structured part, etc.)
            into a dict before constructing this — downstream code
            never sees a JSON string.
    """

    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass(frozen=True)
class ToolCallStarted:
    """Streaming-event marker: the provider stream has begun a tool call.

    Yielded by :meth:`LLMAdapter.get_streaming_response_with_tools` at
    the moment a tool-call signal first appears in the provider stream,
    before the call's arguments have finished accumulating. The
    final :class:`LLMResponse` at end-of-stream remains the source of
    truth for the assembled call(s); ``ToolCallStarted`` is a
    "look up — a tool is coming" signal, not a delivery mechanism.

    Two consumers depend on this contract:

    1. **Honesty layer** (kestrel-sovereign #1042 layer 2): the audit
       hook reads this as a deterministic "stop yielding pre-tool prose"
       signal — the chat client can clear any optimistic text bubble
       once a ``ToolCallStarted`` arrives, rather than letting the
       model's "Saved!" lead-in narrate a tool invocation that may not
       in fact succeed.

    2. **Streaming pipeline** (kestrel-sovereign #1045): a corresponding
       SSE ``"revising"`` event is emitted to the frontend so any
       text streamed before the marker can be discarded.

    **Per-provider emission rules** (the contract every implementing
    adapter must satisfy):

    * **OpenAI / OpenRouter / OpenAI-compatible**: fire on the first
      non-null ``delta.tool_calls`` fragment in the stream. ``id`` and
      ``name`` MAY be absent at this moment — OpenAI's first delta for
      a tool call typically carries only ``index``. Adapters MUST
      populate ``index`` and SHOULD populate ``id`` / ``name`` when
      the same first delta surfaces them; subsequent deltas filling
      those fields do NOT trigger additional ``ToolCallStarted``
      events for the same index.
    * **Anthropic**: fire on ``content_block_start`` with
      ``type="tool_use"``. Both ``id`` and ``name`` are populated at
      this moment.
    * **Google / Vertex (Gemini)**: fire on the first
      ``candidates[].content.parts`` entry containing ``functionCall``.
      ``id`` MAY be ``None`` (Gemini does not always use call ids);
      ``name`` is populated.
    * **Ollama**: fire when the streamed message first contains a
      non-null ``tool_calls`` field. ``id`` and ``name`` populated.

    Adapters that do not stream tool calls leave
    ``get_streaming_response_with_tools`` unimplemented; they never
    emit this marker.

    Attributes:
        index: Position in the assembled tool-calls list. Always
            populated. The framework uses this to dispatch when
            multiple tool calls fire concurrently in the same
            streaming response — the order of ``ToolCallStarted``
            events with distinct ``index`` values defines the order
            of the corresponding entries in the final
            :attr:`LLMResponse.tool_calls`.
        id: Provider-supplied identifier, when known at emission
            time. ``None`` is documented and expected for providers
            (OpenAI's first delta, some Gemini paths) where the id
            arrives later in the stream. The final ``LLMResponse``
            has the resolved id.
        name: Function name, when known at emission time. ``None``
            is documented and expected for the same reason ``id`` is.

    Frozen + hashable so consumers can compare events by identity in
    tests and use them as dict keys when correlating multiple
    concurrent calls.
    """

    index: int
    id: Optional[str] = None
    name: Optional[str] = None


@dataclass
class LLMResponse:
    """Unified response from an LLM adapter.

    Attributes:
        content: Text content of the response. May be ``None`` when the
            response is purely a tool call. May coexist with
            ``tool_calls`` for providers that emit a leading text
            segment ("Let me look that up...") before invoking a tool.
        tool_calls: Function-call requests from the model, normalized
            to OpenAI-style :class:`ToolCall` objects regardless of
            provider. ``None`` (not ``[]``) when the model did not
            request any tools — callers use :attr:`has_tool_calls` to
            check.
        raw: The provider-native response object. Kept for debugging,
            audit trails, and provider-specific introspection that the
            unified shape cannot represent. Not serialized to disk.

        input_tokens: Tokens in the prompt (the *uncached* portion for
            providers with prompt caching). ``None`` when the provider
            does not report usage.
        output_tokens: Tokens in the completion.
        total_tokens: ``input_tokens + output_tokens``, excluding cache
            reads. Computed by the adapter when both halves are known.

        cache_creation_input_tokens: Anthropic prompt-cache: tokens
            *written* to the cache on this call. Either ``0`` (no
            cache write) or ``None`` (provider does not report cache
            usage). Distinct: ``0`` means "we tried but nothing to
            cache", ``None`` means "this provider has no cache
            telemetry".
        cache_read_input_tokens: Anthropic prompt-cache: tokens *read*
            from the cache on this call. Same ``0``-vs-``None``
            distinction as cache_creation_input_tokens.
    """

    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    raw: Any = None

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    cache_creation_input_tokens: Optional[int] = None
    cache_read_input_tokens: Optional[int] = None

    @property
    def has_tool_calls(self) -> bool:
        """True iff the model requested at least one tool call.

        Tested as ``response.has_tool_calls`` rather than
        ``response.tool_calls is not None`` so an empty list (which
        some adapters return after filtering) does not look like a
        real tool-call response.
        """
        return self.tool_calls is not None and len(self.tool_calls) > 0
