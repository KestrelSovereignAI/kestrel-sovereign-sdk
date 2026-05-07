"""Tests for ``DynamicTool.execute``'s ToolResult-aware pass-through.

The wrapper inside ``Feature.get_tools()`` previously wrapped every
return as ``{"success": True, "result": result, "tool": ...}`` —
which hides ``ToolResult.status`` from any consumer reading the
envelope at the top level (the constitutional honesty layer in
#1042). It also makes the value un-serializable because ``ToolResult``
itself isn't JSON-native.

This file pins the new behaviour:
- ``ToolResult`` returns are passed through as ``to_dict()`` with a
  ``tool: <name>`` key appended for dispatch-layer metadata.
- Non-``ToolResult`` returns continue to use the legacy
  ``{success, result, tool}`` wrapping during the migration window;
  once the framework's registry validator (#1042 layer 4b) lands and
  rejects non-``ToolResult`` methods, that path becomes unreachable.
- Exceptions still produce the legacy ``{success: False, error,
  tool}`` shape (handled in PR-E once every tool has migrated).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kestrel_sdk.features.base import Feature, tool
from kestrel_sdk.tools.base import ToolCategory
from kestrel_sdk.tools.result import ToolResult, ToolResultStatus


def _run(coro):
    """Tiny sync runner so tests don't need pytest-asyncio just for two cases."""
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _FixtureFeature(Feature):
    """Real Feature subclass so DynamicTool wraps actual @tool methods."""

    @property
    def tool_description(self) -> str:  # noqa: D401  (single-line)
        return "fixture feature for DynamicTool tests"

    async def initialize(self) -> None:
        """No-op; the fixture doesn't need real lifecycle setup."""
        return None

    @tool(
        name="returns_ok",
        description="Returns a ToolResult.ok",
        category=ToolCategory.UTILITY,
    )
    async def returns_ok(self) -> ToolResult:
        return ToolResult.ok("Saved fact 12345", data={"node_id": "12345"})

    @tool(
        name="returns_failed",
        description="Returns a ToolResult.failed",
        category=ToolCategory.UTILITY,
    )
    async def returns_failed(self) -> ToolResult:
        return ToolResult.failed("memory store returned no node_id")

    @tool(
        name="returns_partial",
        description="Returns a ToolResult.partial",
        category=ToolCategory.UTILITY,
    )
    async def returns_partial(self) -> ToolResult:
        return ToolResult.partial("Saved", "indexing degraded")

    @tool(
        name="returns_legacy_dict",
        description="Returns a legacy dict (pre-migration shape)",
        category=ToolCategory.UTILITY,
    )
    async def returns_legacy_dict(self) -> dict:
        return {"saved": True, "node_id": "abc"}

    @tool(
        name="raises",
        description="Raises an exception",
        category=ToolCategory.UTILITY,
    )
    async def raises(self) -> ToolResult:
        raise RuntimeError("simulated failure")


def _get_dyn_tool(feature: Feature, name: str):
    for t in feature.get_tools():
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not found among {[t.name for t in feature.get_tools()]}")


@pytest.fixture
def feature():
    return _FixtureFeature(agent=None)


class TestToolResultPassThrough:
    """ToolResult returns reach the dispatch layer with status at top level."""

    def test_ok_passthrough_exposes_status_at_top_level(self, feature):
        t = _get_dyn_tool(feature, "returns_ok")
        out = _run(t.execute())
        assert out["status"] == "ok"
        assert out["confirmation"] == "Saved fact 12345"
        assert out["data"] == {"node_id": "12345"}
        # No legacy wrapper keys.
        assert "success" not in out
        assert "result" not in out
        # Dispatch metadata still present.
        assert out["tool"] == "returns_ok"

    def test_failed_passthrough(self, feature):
        t = _get_dyn_tool(feature, "returns_failed")
        out = _run(t.execute())
        assert out["status"] == "error"
        assert out["error"] == "memory store returned no node_id"
        assert "confirmation" not in out
        assert out["tool"] == "returns_failed"

    def test_partial_passthrough_carries_both_halves(self, feature):
        """The whole point of PARTIAL: both confirmation AND error
        reach the audit layer so the LLM is contractually obliged to
        surface both."""
        t = _get_dyn_tool(feature, "returns_partial")
        out = _run(t.execute())
        assert out["status"] == "partial"
        assert out["confirmation"] == "Saved"
        assert out["error"] == "indexing degraded"

    def test_passthrough_is_json_serializable(self, feature):
        """ToolResult itself is not JSON-native — without to_dict()
        conversion the audit layer's json.dumps would fail."""
        t = _get_dyn_tool(feature, "returns_ok")
        out = _run(t.execute())
        # Round-trips through json.
        roundtripped = json.loads(json.dumps(out))
        assert roundtripped["status"] == "ok"


class TestLegacyShapeDuringMigration:
    """Pre-migration tools that still return arbitrary dicts continue
    to work via the legacy wrap. PR-E's registry validator removes
    this path once every tool has migrated."""

    def test_legacy_dict_return_uses_legacy_wrap(self, feature):
        t = _get_dyn_tool(feature, "returns_legacy_dict")
        out = _run(t.execute())
        # Old shape preserved during migration.
        assert out["success"] is True
        assert out["result"] == {"saved": True, "node_id": "abc"}
        assert out["tool"] == "returns_legacy_dict"
        # No status field — that's exactly what the validator will
        # later use to reject the legacy shape.
        assert "status" not in out

    def test_exception_uses_legacy_failure_wrap(self, feature):
        """Exception path still produces the pre-migration failure
        shape. PR-E rewires this to ToolResult.failed once the
        validator is in place."""
        t = _get_dyn_tool(feature, "raises")
        out = _run(t.execute())
        assert out["success"] is False
        assert out["error"] == "simulated failure"
        assert out["tool"] == "raises"
