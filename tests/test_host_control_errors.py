"""The host, not the SDK, decides which tool exceptions are control (#105).

A host running SDK features inside execution custody raises its own
authority, commit and retirement errors through the tool methods it calls.
The SDK ``DynamicTool.execute`` used to turn every exception into a failure
dict, erasing that typed control fact at the inner boundary. With a host
predicate registered, a control error now propagates unchanged; with none,
standalone behaviour is exactly as before.
"""

from __future__ import annotations

from typing import List, Optional

import pytest

from kestrel_sdk.features import host_errors
from kestrel_sdk.features.base import Feature, tool
from kestrel_sdk.features.host_errors import (
    is_host_control_error,
    register_control_error_predicate,
)
from kestrel_sdk.tools.base import AgentTool, ToolCategory


class ControlError(Exception):
    """Stands in for a host's execution-authority error."""


def _is_control(exc: BaseException) -> bool:
    return isinstance(exc, ControlError)


def _everything_is_control(exc: BaseException) -> bool:
    return True


class _RaisingFeature(Feature):
    def __init__(self, agent=None):
        super().__init__(agent)
        self.raised: Optional[BaseException] = None

    @property
    def tool_description(self) -> str:
        return "fixture feature for host control-error tests"

    async def initialize(self) -> None:
        return None

    @tool(
        name="lose_authority",
        description="Raises a host execution-control error",
        category=ToolCategory.UTILITY,
    )
    async def lose_authority(self) -> None:
        self.raised = ControlError("execution authority withdrawn")
        raise self.raised

    @tool(
        name="fail",
        description="Raises an ordinary error",
        category=ToolCategory.UTILITY,
    )
    async def fail(self) -> None:
        raise ValueError("ordinary failure")


@pytest.fixture(autouse=True)
def no_registered_predicate(monkeypatch):
    """Registration is process-global: start each test from none, then restore."""
    monkeypatch.setattr(host_errors, "_control_error_predicate", None)


@pytest.fixture
def feature() -> _RaisingFeature:
    return _RaisingFeature()


def _dyn_tool(feature: Feature, name: str) -> AgentTool:
    return next(t for t in feature.get_tools() if t.name == name)


class TestDynamicToolExecute:
    @pytest.mark.asyncio
    @pytest.mark.parametrize('error_type', [TimeoutError, OSError])
    @pytest.mark.parametrize('withdraw_capability', [False, True])
    async def test_durable_host_receives_the_original_error_before_result_projection(
        self, tmp_path, error_type, withdraw_capability,
    ):
        class DurableHost:
            supports_durable_tool_effects = True

        host = DurableHost()
        original = error_type('Original acknowledgement unavailable after the effect')
        witness = tmp_path / 'original-effect'

        class JournalFeature(_RaisingFeature):
            @tool(name='write_then_fail', description='Preserve the effect boundary error')
            async def write_then_fail(self) -> None:
                witness.write_text('one original effect')
                if withdraw_capability:
                    host.supports_durable_tool_effects = False
                raise original

        feature = JournalFeature(host)
        with pytest.raises(error_type) as raised:
            await _dyn_tool(feature, 'write_then_fail').execute()
        assert raised.value is original
        assert witness.read_text() == 'one original effect'

    @pytest.mark.asyncio
    async def test_registered_control_error_propagates_as_the_same_object(
        self, feature
    ):
        register_control_error_predicate(_is_control)
        with pytest.raises(ControlError) as raised:
            await _dyn_tool(feature, "lose_authority").execute()
        assert raised.value is feature.raised

    @pytest.mark.asyncio
    async def test_ordinary_error_still_becomes_the_failure_dict(self, feature):
        register_control_error_predicate(_is_control)
        out = await _dyn_tool(feature, "fail").execute()
        assert out == {"success": False, "error": "ordinary failure", "tool": "fail"}

    @pytest.mark.asyncio
    async def test_without_a_predicate_control_error_becomes_the_failure_dict(
        self, feature
    ):
        """Backward compatible: the SDK alone never classifies control."""
        out = await _dyn_tool(feature, "lose_authority").execute()
        assert out == {
            "success": False,
            "error": "execution authority withdrawn",
            "tool": "lose_authority",
        }

    @pytest.mark.asyncio
    async def test_predicate_judges_the_original_exception(self, feature):
        seen: List[BaseException] = []

        def recording(exc: BaseException) -> bool:
            seen.append(exc)
            return False

        register_control_error_predicate(recording)
        await _dyn_tool(feature, "lose_authority").execute()
        assert len(seen) == 1
        assert seen[0] is feature.raised


class TestRegistration:
    def test_nothing_is_control_by_default(self):
        assert is_host_control_error(ControlError()) is False
        assert is_host_control_error(ValueError()) is False

    def test_registered_predicate_classifies(self):
        register_control_error_predicate(_is_control)
        assert is_host_control_error(ControlError()) is True
        assert is_host_control_error(ValueError()) is False

    def test_registering_the_same_predicate_again_is_idempotent(self):
        register_control_error_predicate(_is_control)
        register_control_error_predicate(_is_control)
        assert is_host_control_error(ControlError()) is True

    def test_registering_a_different_predicate_raises(self):
        register_control_error_predicate(_is_control)
        with pytest.raises(RuntimeError, match="already registered"):
            register_control_error_predicate(_everything_is_control)
        # The original predicate is still the one in force.
        assert is_host_control_error(ValueError()) is False

    def test_a_non_callable_predicate_is_refused(self):
        with pytest.raises(TypeError, match="callable"):
            register_control_error_predicate("ControlError")  # type: ignore[arg-type]
        assert is_host_control_error(ControlError()) is False

    def test_a_non_boolean_verdict_fails_loudly(self):
        register_control_error_predicate(lambda exc: 1)  # type: ignore[arg-type,return-value]
        with pytest.raises(TypeError, match="boolean"):
            is_host_control_error(ControlError())
