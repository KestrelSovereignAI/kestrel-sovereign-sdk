"""Plugin conformance helpers (SDK 0.8.0+).

This module exists to make third-party LLM provider plugins
(``kestrel-llm-kimi``, ``kestrel-llm-deepseek``, etc.) cheap to
verify against the SDK contract. Plugin authors don't have to
reverse-engineer the streaming-with-tools rules from
:mod:`kestrel_sdk.llm.adapter`'s docstrings — they import these
helpers, drive their adapter against a mock provider client, and
let the helpers assert every contract clause.

The intentional split:

* **Plugin authors own the mock provider client.** Each backend has
  its own event shape (Anthropic typed events, OpenAI delta-indexed
  fragments, custom JSON-line streams). This module does not try to
  abstract over those — that's the abstraction the
  :mod:`kestrel_sdk.llm.adapter.LLMAdapter` contract is the wrong
  size to build on (see ``kestrel-sovereign#1048`` Wave 3
  retrospective).
* **The SDK owns the contract assertions.** Once the adapter has
  produced an ``AsyncIterator[Union[str, ToolCallStarted, LLMResponse]]``
  via :meth:`LLMAdapter.get_streaming_response_with_tools`, the rules
  (one ``ToolCallStarted`` per index, ordering, terminal
  ``LLMResponse``, malformed-JSON sentinel under ``"_raw"``, etc.)
  are independent of how the bytes were produced. The helpers
  enforce them.

Usage from a plugin's test suite::

    import asyncio
    import pytest
    from kestrel_sdk.testing import drain_streaming_with_tools

    @pytest.mark.asyncio
    async def test_my_adapter_emits_tool_call_started():
        adapter = MyAdapter()
        mock_client = my_mock_client_with_one_tool_call(...)
        stream = adapter.get_streaming_response_with_tools(
            client=mock_client,
            model="my-model",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "lookup"}}],
        )
        result = await drain_streaming_with_tools(stream)

        # Contract assertions already ran inside drain_*().
        # Now validate the plugin-specific outcome.
        assert result.text == ""
        assert len(result.tool_starts) == 1
        assert result.tool_starts[0].name == "lookup"
        assert result.final_response.tool_calls[0].arguments == {"q": "hi"}

The helper raises :class:`AssertionError` (the pytest-friendly
shape) the moment a contract clause is violated, with a message
that names the clause so plugin authors get an actionable hint
rather than a deep traceback.

This module ships in the base SDK install (no extras required).
"""

from .conformance import (
    StreamingWithToolsResult,
    assert_response_contract,
    assert_tool_call_started_contract,
    drain_streaming_with_tools,
    drain_streaming_text_only,
)
from .two_axes_contract import (
    TWO_AXES_CONTRACT_SCHEMA,
    TwoAxesContractFixture,
    load_two_axes_contract,
)

__all__ = [
    "StreamingWithToolsResult",
    "TWO_AXES_CONTRACT_SCHEMA",
    "TwoAxesContractFixture",
    "assert_response_contract",
    "assert_tool_call_started_contract",
    "drain_streaming_text_only",
    "drain_streaming_with_tools",
    "load_two_axes_contract",
]
