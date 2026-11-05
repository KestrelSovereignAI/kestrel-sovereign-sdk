"""Completion evidence is provider data, never inferred from response content."""

import asyncio
from dataclasses import asdict, fields, replace
from enum import Enum
from typing import get_args, get_type_hints

import pytest

from kestrel_sdk.llm import (
    SDK_LLM_CONTRACT_VERSION,
    LLMAdapter,
    LLMResponse,
    ToolCall,
    ToolCallStarted,
)
from kestrel_sdk.testing import (
    assert_response_contract,
    drain_streaming_text_only,
    drain_streaming_with_tools,
)


class NativeReason(str, Enum):
    STOP = "stop"


def test_completion_contract_version_is_eight():
    assert SDK_LLM_CONTRACT_VERSION == 8


def test_protocol_error_is_part_of_the_public_lightweight_contract():
    from kestrel_sdk import llm

    assert "LLMProtocolError" in llm.__all__
    assert issubclass(llm.LLMProtocolError, RuntimeError)
    error = llm.LLMProtocolError("Provider sent content after its terminal reason.")
    assert str(error) == "Provider sent content after its terminal reason."


@pytest.mark.parametrize(
    "reason",
    [None, "stop", "tool_calls", "length", "content_filter", "STOP", "MAX_TOKENS",
     "SAFETY", "end_turn", "tool_use", "max_tokens", "future_provider_reason"],
)
def test_response_retains_native_completion_reason_without_classifying_it(reason):
    response = LLMResponse(content="partial or complete", stop_reason=reason)
    assert response.stop_reason == reason
    assert asdict(response)["stop_reason"] == reason
    assert_response_contract(response)


@pytest.mark.parametrize("content", [None, "", "This looks like a finished answer."])
def test_content_usage_and_raw_shape_do_not_create_completion_evidence(content):
    response = LLMResponse(
        content=content,
        tool_calls=[ToolCall(id="call", name="write", arguments={})],
        raw={"choices": [{"finish_reason": "stop"}]},
        input_tokens=1,
        output_tokens=2,
        total_tokens=3,
    )
    assert response.stop_reason is None


def test_completion_field_is_appended_without_reassigning_positional_arguments():
    raw = object()
    response = LLMResponse("text", None, raw, 1, 2, 3, 4, 5, [], [])
    assert response.raw is raw
    assert response.cache_read_input_tokens == 5
    assert response.citations == []
    assert response.server_tool_calls == []
    assert response.stop_reason is None
    assert fields(LLMResponse)[-1].name == "stop_reason"


def test_structured_content_projection_preserves_completion_and_usage_metadata():
    raw = object()
    response = LLMResponse(
        tool_calls=[ToolCall(id="schema", name="structured", arguments={"value": 1})],
        raw=raw,
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
        cache_creation_input_tokens=2,
        cache_read_input_tokens=3,
        stop_reason="tool_use",
    )
    projected = replace(response, content='{"value": 1}', tool_calls=None)
    assert projected.stop_reason == "tool_use"
    assert projected.raw is raw
    assert (projected.input_tokens, projected.output_tokens, projected.total_tokens) == (11, 7, 18)
    assert (projected.cache_creation_input_tokens, projected.cache_read_input_tokens) == (2, 3)
    assert response.tool_calls is not None


def test_plain_stream_annotation_includes_a_terminal_response():
    stream = get_type_hints(LLMAdapter.get_streaming_response)["return"]
    (item,) = get_args(stream)
    assert set(get_args(item)) == {str, LLMResponse}


def test_tool_stream_annotation_includes_markers_and_a_terminal_response():
    stream = get_type_hints(LLMAdapter.get_streaming_response_with_tools)["return"]
    (item,) = get_args(stream)
    assert set(get_args(item)) == {str, ToolCallStarted, LLMResponse}


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks", [[], [""], ["apparently complete text"]])
async def test_conformance_rejects_normal_eof_without_a_terminal_response(chunks):
    async def stream():
        for chunk in chunks:
            yield chunk

    with pytest.raises(AssertionError, match="terminal LLMResponse"):
        await drain_streaming_with_tools(stream())


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "stop", "length", "future_provider_reason"])
async def test_terminal_envelope_is_evidence_not_a_completion_policy_decision(reason):
    terminal = LLMResponse(content="text", stop_reason=reason)

    async def stream():
        yield "text"
        yield terminal

    result = await drain_streaming_with_tools(stream())
    assert result.text == "text"
    assert result.final_response is terminal


@pytest.mark.parametrize("reason", [1, True, [], {}, "", NativeReason.STOP])
def test_response_conformance_rejects_non_string_stop_metadata(reason):
    response = LLMResponse()
    response.stop_reason = reason
    with pytest.raises(AssertionError, match="stop_reason"):
        assert_response_contract(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("transport interrupted"), asyncio.CancelledError()])
async def test_stream_failure_is_propagated_without_a_terminal_result(error):
    async def stream():
        yield "partial"
        raise error

    with pytest.raises(type(error)) as raised:
        await drain_streaming_with_tools(stream())
    assert raised.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks", [[], [""], ["apparently complete text"]])
async def test_plain_conformance_rejects_eof_without_terminal_response(chunks):
    async def stream():
        for chunk in chunks:
            yield chunk

    with pytest.raises(AssertionError, match="terminal LLMResponse"):
        await drain_streaming_text_only(stream())


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "stop", "length", "future_provider_reason"])
async def test_plain_conformance_accepts_terminal_metadata_without_rendering_it(reason):
    async def stream():
        yield "text"
        yield LLMResponse(content="text", stop_reason=reason)

    assert await drain_streaming_text_only(stream()) == "text"


@pytest.mark.asyncio
async def test_plain_conformance_rejects_tool_intent():
    async def stream():
        yield ToolCallStarted(index=0)

    with pytest.raises(AssertionError, match="text-only output"):
        await drain_streaming_text_only(stream())


@pytest.mark.asyncio
async def test_plain_conformance_rejects_assembled_tool_calls():
    async def stream():
        yield LLMResponse(tool_calls=[ToolCall(id="call", name="write", arguments={})])

    with pytest.raises(AssertionError, match="text-only output"):
        await drain_streaming_text_only(stream())
