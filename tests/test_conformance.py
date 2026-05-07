"""Tests for kestrel_sdk.testing — the plugin-author conformance helpers.

These are meta-tests: they verify the helpers correctly accept
contract-conforming streams and correctly reject contract violations.
Plugin authors will run the helpers from their own test suites; the
SDK's own coverage is here.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, List, Optional

import pytest

from kestrel_sdk.llm import LLMResponse, ToolCall, ToolCallStarted
from kestrel_sdk.testing import (
    StreamingWithToolsResult,
    assert_response_contract,
    assert_tool_call_started_contract,
    drain_streaming_text_only,
    drain_streaming_with_tools,
)


async def _stream_from(items: List) -> AsyncIterator:
    """Build a typed-union stream from an explicit list of items."""
    for item in items:
        yield item


def _drive(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# drain_streaming_with_tools — happy paths
# ---------------------------------------------------------------------------


class TestDrainStreamingWithToolsHappy:
    def test_text_only_stream_no_final_response_is_allowed(self):
        """Pinned: pure-text streams MAY terminate without a final
        LLMResponse (the contract reserves the terminal LLMResponse
        for tool-call responses)."""
        items = ["hello ", "world"]
        result = _drive(drain_streaming_with_tools(_stream_from(items)))
        assert result.text == "hello world"
        assert result.text_chunks == ["hello ", "world"]
        assert result.tool_starts == []
        assert result.final_response is None

    def test_single_tool_call_with_marker_and_response(self):
        items = [
            "Let me look that up. ",
            ToolCallStarted(index=0, id="call_1", name="lookup"),
            LLMResponse(
                content="Let me look that up. ",
                tool_calls=[
                    ToolCall(id="call_1", name="lookup", arguments={"q": "hi"}),
                ],
            ),
        ]
        result = _drive(drain_streaming_with_tools(_stream_from(items)))
        assert result.text == "Let me look that up. "
        assert len(result.tool_starts) == 1
        assert result.tool_starts[0].index == 0
        assert result.final_response is not None
        assert result.final_response.has_tool_calls

    def test_multiple_concurrent_tool_calls(self):
        items = [
            ToolCallStarted(index=0, id="c0", name="fn0"),
            ToolCallStarted(index=1, id="c1", name="fn1"),
            ToolCallStarted(index=2, id="c2", name="fn2"),
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c0", name="fn0", arguments={}),
                    ToolCall(id="c1", name="fn1", arguments={}),
                    ToolCall(id="c2", name="fn2", arguments={}),
                ],
            ),
        ]
        result = _drive(drain_streaming_with_tools(_stream_from(items)))
        assert [s.index for s in result.tool_starts] == [0, 1, 2]
        assert len(result.final_response.tool_calls) == 3

    def test_marker_with_none_id_and_name_accepted(self):
        """The OpenAI-first-delta case: marker arrives with index but
        no id/name yet. Contract accepts."""
        items = [
            ToolCallStarted(index=0, id=None, name=None),
            LLMResponse(
                tool_calls=[
                    ToolCall(id="filled-in-later", name="fn", arguments={}),
                ],
            ),
        ]
        result = _drive(drain_streaming_with_tools(_stream_from(items)))
        assert result.tool_starts[0].id is None
        assert result.tool_starts[0].name is None


# ---------------------------------------------------------------------------
# drain_streaming_with_tools — contract violations
# ---------------------------------------------------------------------------


class TestDrainStreamingWithToolsViolations:
    def test_unknown_yielded_type_is_rejected(self):
        """Plugins MUST NOT smuggle adapter-specific data through the
        streaming channel."""
        items = ["text", 42, ToolCallStarted(index=0)]
        with pytest.raises(AssertionError, match="unexpected type"):
            _drive(drain_streaming_with_tools(_stream_from(items)))

    def test_two_markers_with_same_index_are_rejected(self):
        items = [
            ToolCallStarted(index=0, id="a"),
            ToolCallStarted(index=0, id="b"),
            LLMResponse(
                tool_calls=[ToolCall(id="a", name="n", arguments={})]
            ),
        ]
        with pytest.raises(AssertionError, match="exactly one marker"):
            _drive(drain_streaming_with_tools(_stream_from(items)))

    def test_value_after_terminal_response_is_rejected(self):
        items = [
            LLMResponse(content="done"),
            "stragglers",
        ]
        with pytest.raises(AssertionError, match="LAST item"):
            _drive(drain_streaming_with_tools(_stream_from(items)))

    def test_marker_without_terminal_response_is_rejected(self):
        items = [ToolCallStarted(index=0, id="x", name="y")]
        with pytest.raises(AssertionError, match="MUST yield exactly one"):
            _drive(drain_streaming_with_tools(_stream_from(items)))

    def test_marker_count_mismatch_with_tool_calls_is_rejected(self):
        items = [
            ToolCallStarted(index=0),
            LLMResponse(
                tool_calls=[
                    ToolCall(id="a", name="n1", arguments={}),
                    ToolCall(id="b", name="n2", arguments={}),
                ],
            ),
        ]
        with pytest.raises(AssertionError, match="count.*does not equal"):
            _drive(drain_streaming_with_tools(_stream_from(items)))

    def test_marker_with_no_corresponding_tool_call_is_rejected(self):
        items = [
            ToolCallStarted(index=0),
            LLMResponse(content="text-only after a marker"),
        ]
        with pytest.raises(AssertionError, match="MUST appear in the"):
            _drive(drain_streaming_with_tools(_stream_from(items)))


# ---------------------------------------------------------------------------
# drain_streaming_text_only
# ---------------------------------------------------------------------------


class TestDrainStreamingTextOnly:
    def test_text_only_returns_concatenated_text(self):
        items = ["hello ", "there ", "world"]
        text = _drive(drain_streaming_text_only(_stream_from(items)))
        assert text == "hello there world"

    def test_marker_in_text_only_stream_is_rejected(self):
        items = ["hello", ToolCallStarted(index=0)]
        with pytest.raises(AssertionError, match="text-only output"):
            _drive(drain_streaming_text_only(_stream_from(items)))

    def test_response_in_text_only_stream_is_rejected(self):
        items = ["hello", LLMResponse(content="hello")]
        with pytest.raises(AssertionError, match="text-only output"):
            _drive(drain_streaming_text_only(_stream_from(items)))


# ---------------------------------------------------------------------------
# assert_tool_call_started_contract
# ---------------------------------------------------------------------------


class TestAssertToolCallStartedContract:
    def test_valid_marker_passes(self):
        # Should not raise.
        assert_tool_call_started_contract(
            ToolCallStarted(index=0, id="c", name="n")
        )
        assert_tool_call_started_contract(ToolCallStarted(index=0))

    def test_negative_index_is_rejected(self):
        marker = ToolCallStarted(index=-1)
        with pytest.raises(AssertionError, match="non-negative"):
            assert_tool_call_started_contract(marker)

    def test_empty_string_id_is_rejected(self):
        marker = ToolCallStarted(index=0, id="")
        with pytest.raises(AssertionError, match="never the empty string"):
            assert_tool_call_started_contract(marker)

    def test_empty_string_name_is_rejected(self):
        marker = ToolCallStarted(index=0, name="")
        with pytest.raises(AssertionError, match="never the empty string"):
            assert_tool_call_started_contract(marker)


# ---------------------------------------------------------------------------
# assert_response_contract
# ---------------------------------------------------------------------------


class TestAssertResponseContract:
    def test_text_only_response_passes(self):
        assert_response_contract(LLMResponse(content="hello"))

    def test_response_with_tool_calls_passes(self):
        assert_response_contract(
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c", name="n", arguments={"k": "v"}),
                ],
            )
        )

    def test_empty_tool_calls_list_is_rejected(self):
        """tool_calls=[] is not a valid shape — adapters that filtered
        all calls MUST set tool_calls=None."""
        resp = LLMResponse(tool_calls=[])
        with pytest.raises(AssertionError, match="must be None when empty"):
            assert_response_contract(resp)

    def test_tool_call_with_empty_id_is_rejected(self):
        resp = LLMResponse(
            tool_calls=[ToolCall(id="", name="n", arguments={})]
        )
        with pytest.raises(AssertionError, match="non-empty string"):
            assert_response_contract(resp)

    def test_tool_call_with_empty_name_is_rejected(self):
        resp = LLMResponse(
            tool_calls=[ToolCall(id="c", name="", arguments={})]
        )
        with pytest.raises(AssertionError, match="non-empty string"):
            assert_response_contract(resp)

    def test_legacy_raw_sentinel_is_caught_with_migration_hint(self):
        """Plugins still on the pre-0.7.0 ``raw`` sentinel get a
        clear migration message rather than a confusing downstream
        parse error."""
        resp = LLMResponse(
            tool_calls=[
                ToolCall(id="c", name="n", arguments={"raw": "bad json"})
            ]
        )
        with pytest.raises(AssertionError, match="renamed this to ``'_raw'``"):
            assert_response_contract(resp)

    def test_underscore_raw_sentinel_passes(self):
        """The current sentinel shape is valid."""
        resp = LLMResponse(
            tool_calls=[
                ToolCall(id="c", name="n", arguments={"_raw": "bad json"})
            ]
        )
        assert_response_contract(resp)
