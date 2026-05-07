"""Conformance assertions for the LLM adapter streaming contract.

Implementation notes:

* Every assertion is phrased so the failure message names the
  contract clause being violated. Plugin authors hitting one of
  these in CI should be able to find the exact docstring in
  :mod:`kestrel_sdk.llm.adapter` (or :mod:`kestrel_sdk.llm.response`)
  that motivates the rule.

* The helpers are deliberately *strict* on the parts the
  framework's downstream consumers will rely on (one
  ``ToolCallStarted`` per index, terminal ``LLMResponse``, no
  ``content=None`` system messages slipping through), and *lenient*
  on the parts the contract leaves to adapter discretion (whether
  text and ``ToolCallStarted`` events interleave, whether token
  accounting is reported, whether ``id`` / ``name`` are populated
  on the marker — these vary by provider per the adapter docstring).

* The helpers do not check the SHAPE of the input stream beyond
  ``Union[str, ToolCallStarted, LLMResponse]``. Any other yielded
  type is reported as a contract violation immediately. The plugin
  author doesn't need to know which SDK release defined what — the
  current ``Union`` is the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, List, Optional, Union

from kestrel_sdk.llm import LLMResponse, ToolCall, ToolCallStarted

StreamItem = Union[str, ToolCallStarted, LLMResponse]


@dataclass
class StreamingWithToolsResult:
    """Drained output of an adapter's streaming-with-tools call.

    Plugin tests typically construct one of these via
    :func:`drain_streaming_with_tools` and then assert
    plugin-specific properties on top of the
    contract-already-validated structure.

    Attributes:
        text: The concatenated string chunks yielded during the
            stream, in order. Equivalent to ``"".join(<text items>)``.
            May be ``""`` for tool-only streams.
        text_chunks: Each individual ``str`` item the stream
            yielded, preserved in order. Useful when the test wants
            to assert chunk boundaries (e.g. "the model emitted
            three deltas before the tool call fired").
        tool_starts: Every ``ToolCallStarted`` marker the stream
            yielded, in order. Index uniqueness has already been
            asserted by the drain helper.
        final_response: The single terminal ``LLMResponse``, or
            ``None`` if the stream completed text-only without one.
            Pure-text streams are explicitly allowed to terminate
            without a final ``LLMResponse``; tool-call streams MUST
            yield exactly one.
    """

    text: str = ""
    text_chunks: List[str] = field(default_factory=list)
    tool_starts: List[ToolCallStarted] = field(default_factory=list)
    final_response: Optional[LLMResponse] = None


async def drain_streaming_with_tools(
    stream: AsyncIterator[StreamItem],
) -> StreamingWithToolsResult:
    """Drain an :meth:`~kestrel_sdk.llm.LLMAdapter.get_streaming_response_with_tools`
    stream and validate the contract end-to-end.

    Validates (and raises :class:`AssertionError` with a
    contract-clause-named message on the first violation):

    * Every yielded item is ``str``, :class:`ToolCallStarted`, or
      :class:`LLMResponse`. Anything else is a contract violation —
      adapters MUST NOT smuggle adapter-specific data through the
      streaming channel.
    * At most one terminal ``LLMResponse``. The contract says
      *exactly* one when the response includes tool calls; the
      drain helper enforces *at most* one and exposes the count to
      the caller via ``final_response is not None``.
    * The ``LLMResponse``, when present, is the LAST item in the
      stream. Anything yielded after it is a violation (the
      contract pins the response as terminal so consumers can rely
      on its presence to know the stream is complete).
    * No two ``ToolCallStarted`` markers share the same ``index``.
      The contract requires exactly one marker per distinct
      tool-call index.
    * The relative order of distinct ``ToolCallStarted.index``
      values matches the order of the corresponding entries in
      ``LLMResponse.tool_calls``. The framework dispatches by
      ``index``; an out-of-order marker would mislead a consumer
      that read marker order to anticipate which call was about to
      fire.
    * If the stream contains :class:`ToolCallStarted` events, the
      terminal ``LLMResponse`` MUST be present and MUST have at
      least one tool call (otherwise the marker announced a tool
      call that never landed in the assembled response).
    * If a ``LLMResponse`` is present and has tool calls, every
      ``index`` in ``tool_calls`` (matched positionally) is paired
      with a ``ToolCallStarted`` event. Adapters that yielded the
      final response without any markers are violating the
      consumer-half contract — the honesty layer can't gate prose
      without the markers.

    Per-marker field validation (id / name nullability) is delegated
    to :func:`assert_tool_call_started_contract`. This function does
    not assert plugin-specific properties — that's the caller's
    job after drain returns.
    """
    out = StreamingWithToolsResult()

    async for item in stream:
        if out.final_response is not None:
            raise AssertionError(
                "Stream yielded a value after the terminal LLMResponse. "
                "Contract: LLMResponse is the LAST item; consumers rely "
                "on its presence to know the stream is complete. Got: "
                f"{type(item).__name__}={item!r}"
            )
        if isinstance(item, str):
            out.text_chunks.append(item)
            out.text += item
            continue
        if isinstance(item, ToolCallStarted):
            assert_tool_call_started_contract(item)
            for prev in out.tool_starts:
                if prev.index == item.index:
                    raise AssertionError(
                        f"Stream yielded two ToolCallStarted events "
                        f"with index={item.index}. Contract: exactly "
                        f"one marker per distinct tool-call index. "
                        f"Earlier: {prev!r}; current: {item!r}"
                    )
            out.tool_starts.append(item)
            continue
        if isinstance(item, LLMResponse):
            assert_response_contract(item)
            out.final_response = item
            continue
        raise AssertionError(
            f"Stream yielded an unexpected type {type(item).__name__}. "
            "Contract: get_streaming_response_with_tools yields "
            "Union[str, ToolCallStarted, LLMResponse]. Plugins MUST "
            "NOT smuggle adapter-specific data through the streaming "
            "channel; carry it on LLMResponse.raw instead."
        )

    # Cross-cutting checks once the stream has drained.
    final_tool_calls = (
        out.final_response.tool_calls
        if out.final_response is not None
        else None
    ) or []

    if final_tool_calls and not out.tool_starts:
        # Codex review caught this gap: an adapter that yields a
        # terminal LLMResponse with tool_calls but no preceding
        # markers violates the consumer-half contract. The honesty
        # layer (#1042 layer 2) and the streaming "revising" event
        # (#1045) both gate on the marker — without one, pre-tool
        # prose leaks. The conformance suite MUST reject this.
        raise AssertionError(
            "Stream's terminal LLMResponse has tool_calls but no "
            "ToolCallStarted markers were yielded earlier. Contract: "
            "every assembled tool call MUST have a corresponding "
            "marker so downstream honesty/revising consumers can "
            "gate pre-tool prose. Final tool_calls: "
            f"{[(tc.id, tc.name) for tc in final_tool_calls]}"
        )

    if out.tool_starts:
        if out.final_response is None:
            raise AssertionError(
                "Stream emitted ToolCallStarted markers but never yielded "
                "a terminal LLMResponse. Contract: tool-call streams "
                "MUST yield exactly one LLMResponse at end-of-stream "
                f"so consumers see the assembled call. Markers seen: "
                f"{len(out.tool_starts)} (indices "
                f"{[s.index for s in out.tool_starts]})."
            )
        if not out.final_response.has_tool_calls:
            raise AssertionError(
                "Stream emitted ToolCallStarted markers but the terminal "
                "LLMResponse has no tool_calls. Contract: a marker "
                "announces a tool call that MUST appear in the "
                "assembled response. Markers seen: "
                f"{[s.index for s in out.tool_starts]}; final "
                f"tool_calls: {out.final_response.tool_calls!r}"
            )

        # Count alignment: every assembled tool_call has a marker.
        if len(out.tool_starts) != len(final_tool_calls):
            raise AssertionError(
                f"ToolCallStarted count ({len(out.tool_starts)}) does "
                f"not equal final tool_calls count "
                f"({len(final_tool_calls)}). Contract: every assembled "
                "tool call MUST have a corresponding ToolCallStarted "
                "marker yielded earlier."
            )

        # Order alignment: the position of a marker in stream order
        # MUST equal the position of the corresponding tool call in
        # ``LLMResponse.tool_calls``. This is the contract's load-
        # bearing claim — consumers (the audit hook in particular)
        # treat marker[i] as "TC[i] is about to fire".
        #
        # Note: ``marker.index`` is provider-specific (Anthropic uses
        # content_block_index which is sparse when text blocks
        # interleave; OpenAI uses delta tool_call index which is
        # positional). The conformance suite pins the ORDER invariant,
        # not the literal-index invariant — so the assertion below
        # walks the streams positionally and uses ``id`` to verify
        # alignment when the marker carries one. Markers with id=None
        # (the OpenAI-first-delta MAY-BE-NONE case) are skipped from
        # the id-correlation check; only their count and stream
        # position are pinned by the surrounding count-equality.
        for i, (marker, tc) in enumerate(zip(out.tool_starts, final_tool_calls)):
            if marker.id is not None and marker.id != tc.id:
                raise AssertionError(
                    f"ToolCallStarted at stream position {i} "
                    f"(id={marker.id!r}) does not match the tool call "
                    f"at the same position in tool_calls "
                    f"(id={tc.id!r}). Contract: marker stream order "
                    "MUST equal tool_calls assembled order — consumers "
                    "treat marker[i] as 'tool_calls[i] is about to "
                    "fire'."
                )
            if marker.name is not None and marker.name != tc.name:
                raise AssertionError(
                    f"ToolCallStarted at stream position {i} "
                    f"(name={marker.name!r}) does not match the tool "
                    f"call at the same position in tool_calls "
                    f"(name={tc.name!r}). Contract: see id mismatch above."
                )

    return out


async def drain_streaming_text_only(
    stream: AsyncIterator[StreamItem],
) -> str:
    """Drain a streaming response that the test asserts is text-only
    and return the concatenated text. Raises :class:`AssertionError`
    if the stream yields any non-string items.

    Use this when the test scenario specifies "no tool call should
    fire" (e.g. asserting a refusal flow stays in text mode); pairing
    with :func:`drain_streaming_with_tools` for tool flows keeps the
    test intent explicit at the call site rather than buried in a
    type guard.
    """
    text_chunks: List[str] = []
    async for item in stream:
        if not isinstance(item, str):
            raise AssertionError(
                f"Stream yielded {type(item).__name__}={item!r} but the "
                "test scenario specified text-only output. Use "
                "drain_streaming_with_tools when the scenario allows "
                "tool calls."
            )
        text_chunks.append(item)
    return "".join(text_chunks)


def assert_tool_call_started_contract(marker: ToolCallStarted) -> None:
    """Validate a single :class:`ToolCallStarted` against the contract.

    The wire-shape rules are mostly enforced by the dataclass itself
    (frozen, ``index: int`` required, ``id`` / ``name`` Optional[str]).
    This helper covers the additional invariants the docstring
    documents but Python's type system does not:

    * ``index`` MUST be a non-negative integer. Adapters use it as
      both a stream-order signal and a positional index into
      ``LLMResponse.tool_calls``; negative values are not meaningful.
    * Both ``id`` and ``name`` MAY be None (the OpenAI-first-delta
      and Gemini-id-absent cases). The empty string is NOT a valid
      placeholder for "unknown" — adapters MUST emit ``None``
      explicitly. This rule keeps consumers from having to
      special-case truthiness on top of ``is None`` checks.
    """
    if not isinstance(marker.index, int):
        raise AssertionError(
            f"ToolCallStarted.index must be an int, got "
            f"{type(marker.index).__name__}={marker.index!r}"
        )
    if marker.index < 0:
        raise AssertionError(
            f"ToolCallStarted.index must be non-negative, got "
            f"{marker.index}. Negative indices break ordering "
            "assumptions in framework consumers."
        )
    if marker.id == "":
        raise AssertionError(
            "ToolCallStarted.id must be either a non-empty string or "
            "None — never the empty string. Adapters that don't yet "
            "know the id at emission time MUST emit None so consumers "
            "can use ``is None`` rather than truthiness checks."
        )
    if marker.name == "":
        raise AssertionError(
            "ToolCallStarted.name must be either a non-empty string "
            "or None — never the empty string. Same reasoning as id."
        )


def assert_response_contract(response: LLMResponse) -> None:
    """Validate a single :class:`LLMResponse` against the contract.

    Mostly mirrors what the dataclass already enforces; the helper
    adds the cross-field rules:

    * ``tool_calls`` is either ``None`` (no tools called) or a
      non-empty list. The empty-list case is reserved for adapters
      that filtered all calls and downgraded to text-only — those
      MUST set ``tool_calls=None``. ``has_tool_calls`` already
      depends on this distinction; ``[]`` here would silently
      misreport.
    * Each :class:`ToolCall` in ``tool_calls`` has the contract
      shape: ``id`` non-empty (the framework echoes it back on the
      next turn), ``name`` non-empty, ``arguments`` a dict.
    * The ``"_raw"`` malformed-JSON sentinel is accepted as a valid
      argument shape. The contract reserves the underscore-prefixed
      ``_raw`` key for parse-failure fallback but does NOT forbid
      tool schemas from having a real argument named ``raw`` (or
      anything else) — that's between the tool author and the model.
      Conformance does not try to detect the pre-0.7.0 sentinel
      rename; that's a one-off migration concern, not a per-call
      check, and a heuristic at this layer would generate false
      positives for tools whose schema legitimately includes a
      ``raw`` parameter.
    """
    if response.tool_calls is not None:
        if not isinstance(response.tool_calls, list):
            raise AssertionError(
                f"LLMResponse.tool_calls must be a list or None, got "
                f"{type(response.tool_calls).__name__}"
            )
        if len(response.tool_calls) == 0:
            raise AssertionError(
                "LLMResponse.tool_calls must be None when empty, not [] "
                "— `has_tool_calls` would silently report False either "
                "way, but downstream code that does `if "
                "response.tool_calls:` distinguishes the cases. Adapters "
                "that filtered all calls MUST set tool_calls=None."
            )
        for i, tc in enumerate(response.tool_calls):
            if not isinstance(tc, ToolCall):
                raise AssertionError(
                    f"LLMResponse.tool_calls[{i}] must be a ToolCall, "
                    f"got {type(tc).__name__}"
                )
            if not tc.id:
                raise AssertionError(
                    f"ToolCall[{i}].id must be a non-empty string. "
                    "The framework echoes this id back on the next "
                    "turn to match tool_call_output to the call; an "
                    "empty id breaks that match."
                )
            if not tc.name:
                raise AssertionError(
                    f"ToolCall[{i}].name must be a non-empty string."
                )
            if not isinstance(tc.arguments, dict):
                raise AssertionError(
                    f"ToolCall[{i}].arguments must be a dict, got "
                    f"{type(tc.arguments).__name__}. Adapters parse "
                    "the provider-native form (JSON string, structured "
                    "part) into a dict before constructing the "
                    "ToolCall — downstream code never sees a JSON "
                    "string. Use the malformed-JSON sentinel "
                    "``{'_raw': '<accumulated>'}`` when parsing fails."
                )
            # Note: ``"raw"`` as an argument key is NOT rejected. The
            # contract reserves ``"_raw"`` (underscore-prefixed) for
            # the malformed-JSON sentinel; an adapter that uses
            # ``"raw"`` as a real parameter name (e.g. for a tool
            # whose schema has a ``raw: str`` parameter) is
            # conforming. The pre-0.7.0 sentinel rename is a one-off
            # migration concern handled at adapter-implementation
            # time, not per-call here.
