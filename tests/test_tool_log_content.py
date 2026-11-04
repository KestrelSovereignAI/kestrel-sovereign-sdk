"""The SDK tool wrapper logs non-content facts only (#105).

Core-hosted SDK features keep the SDK ``DynamicTool``, so its log lines reach
host logs. A tool's exception can quote what it failed on, and a rejected
argument is model-authored text: neither may appear in any log record. The
model-facing error text is unchanged.
"""

from __future__ import annotations

import logging

import pytest

from kestrel_sdk.features.base import Feature, tool
from kestrel_sdk.tools.base import AgentTool, ToolCategory
from kestrel_sdk.tools.result import ToolResult

MARKER = "MARKER-7f3a-caller-content"


class _ContentFeature(Feature):
    @property
    def tool_description(self) -> str:
        return "fixture feature for log-content tests"

    async def initialize(self) -> None:
        return None

    @tool(
        name="lookup",
        description="Fails quoting its argument",
        category=ToolCategory.UTILITY,
    )
    async def lookup(self, query: str) -> ToolResult:
        raise RuntimeError(f"lookup failed for {query}")

    @tool(
        name="count",
        description="Takes an integer",
        category=ToolCategory.UTILITY,
    )
    async def count(self, limit: int) -> ToolResult:
        return ToolResult.ok("counted")


def _dyn_tool(name: str) -> AgentTool:
    return next(t for t in _ContentFeature(None).get_tools() if t.name == name)


def _assert_marker_absent(caplog: pytest.LogCaptureFixture) -> None:
    assert caplog.records, "the wrapper is expected to log the failure"
    for record in caplog.records:
        assert MARKER not in record.getMessage()
        assert MARKER not in repr(record.args)
        assert MARKER not in (record.exc_text or "")
    assert MARKER not in caplog.text


@pytest.mark.asyncio
async def test_tool_exception_message_is_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    out = await _dyn_tool("lookup").execute(query=MARKER)

    assert out["error"] == f"lookup failed for {MARKER}"
    _assert_marker_absent(caplog)
    assert any(
        "lookup" in record.getMessage()
        and "RuntimeError" in record.getMessage()
        and "chars=" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_rejected_argument_value_is_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    out = await _dyn_tool("count").execute(limit=MARKER)

    assert out["status"] == "error"
    assert MARKER in out["error"]
    _assert_marker_absent(caplog)
    assert any(
        "count" in record.getMessage()
        and "'limit'" in record.getMessage()
        and "'integer'" in record.getMessage()
        for record in caplog.records
    )
