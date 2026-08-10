"""Public import and compatibility guarantees for row-1 SDK contracts."""

from __future__ import annotations

import subprocess
import sys
from typing import get_type_hints

import pytest

import kestrel_sdk
import kestrel_sdk.features as feature_contracts
import kestrel_sdk.operator as operator_contracts
from kestrel_sdk.features import Feature, HostFeature


OPERATOR_EXPORTS = {
    "ArtifactAuthorizationAction",
    "ArtifactRecord",
    "CapabilityDescriptor",
    "ExecutionTargetDescriptor",
    "ExecutionTargetReference",
    "ExecutionTargetResolver",
    "ExternalEngineJobLink",
    "ImmutableJSON",
    "JSONScalar",
    "MAX_OPERATOR_CONTEXT_LIFETIME",
    "OperatorAuthorizationError",
    "OperatorContext",
    "RUN_ATTACH_ACTION",
    "RUN_LAUNCH_ACTION",
    "RUN_READ_ACTION",
    "RunAttempt",
    "RunConflictError",
    "RunControl",
    "RunControlAction",
    "RunLaunch",
    "RunNotFoundError",
    "RunPage",
    "RunQuery",
    "RunRecord",
    "RunService",
    "RunSource",
    "RunStage",
    "RunState",
    "ServiceDescriptor",
    "ServiceReference",
    "ServiceRegistration",
    "ServiceRequirement",
    "ServiceResolver",
    "ServiceScope",
}

TOP_LEVEL_OPERATOR_EXPORTS = OPERATOR_EXPORTS

FEATURE_CONTRIBUTION_EXPORTS = {
    "ContributionContractError",
    "ContributionResult",
    "FeatureContributionSet",
    "FeaturePermissionDefaults",
    "PermissionLevel",
    "ServiceContributions",
    "SetupFlow",
    "SetupStep",
    "SetupStepClassification",
    "SetupStepContext",
    "SetupStepContributions",
    "SetupStepRegistration",
    "WaitProviderContributions",
    "WaitProviderRegistration",
    "WorkflowActor",
    "WorkflowContributions",
    "WorkflowRegistration",
    "await_contribution_result",
    "normalize_setup_flow",
    "order_setup_step_registrations",
    "validate_feature_contributions",
}


def test_operator_package_exports_complete_contract_surface() -> None:
    assert OPERATOR_EXPORTS.issubset(operator_contracts.__all__)
    for name in OPERATOR_EXPORTS:
        assert hasattr(operator_contracts, name), name


def test_top_level_selectively_reexports_canonical_operator_contracts() -> None:
    assert TOP_LEVEL_OPERATOR_EXPORTS.issubset(kestrel_sdk.__all__)
    for name in TOP_LEVEL_OPERATOR_EXPORTS:
        assert getattr(kestrel_sdk, name) is getattr(operator_contracts, name)


def test_feature_package_exports_contribution_contracts() -> None:
    assert FEATURE_CONTRIBUTION_EXPORTS.issubset(feature_contracts.__all__)
    for name in FEATURE_CONTRIBUTION_EXPORTS:
        assert hasattr(feature_contracts, name), name


def test_run_contract_surface_has_one_canonical_name_per_model() -> None:
    for redundant_alias in {
        "RunLaunchEnvelope",
        "RunControlRequest",
        "ExternalJobLink",
    }:
        assert redundant_alias not in operator_contracts.__all__
        assert not hasattr(operator_contracts, redundant_alias)


def test_sdk_contract_imports_have_no_runtime_framework_dependency() -> None:
    blocked_roots = {
        "fastapi",
        "kestrel_sovereign",
        "kestrel_workflows",
        "kestrel_talon",
        "kestrel_flight",
        "kestrel_eye",
        "kestrel_feature_workflows",
        "kestrel_feature_talon",
        "kestrel_feature_flight",
        "kestrel_feature_eye",
        "sovereign",
        "workflows",
        "talon",
        "flight",
        "eye",
    }
    script = f"""
import importlib.abc
import sys

blocked = {blocked_roots!r}

class BlockFrameworkImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.', 1)[0] in blocked:
            raise ImportError(f'forbidden framework import: {{fullname}}')
        return None

sys.meta_path.insert(0, BlockFrameworkImports())
import kestrel_sdk
import kestrel_sdk.features
import kestrel_sdk.features.contributions
import kestrel_sdk.operator
assert blocked.isdisjoint(sys.modules)
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_feature_base_modules_do_not_runtime_import_contribution_models() -> None:
    script = """
import importlib.abc
import sys

class BlockContributionImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'kestrel_sdk.features.contributions':
            raise ImportError('base class imported contribution models at runtime')
        return None

sys.meta_path.insert(0, BlockContributionImport())
import kestrel_sdk.features.base
import kestrel_sdk.features.host_base
assert 'kestrel_sdk.features.contributions' not in sys.modules
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_historic_base_module_ui_annotation_resolves_at_runtime() -> None:
    from kestrel_sdk.features.base import UIContributions as FeatureUIContributions
    from kestrel_sdk.features.host_base import UIContributions as HostUIContributions

    assert FeatureUIContributions is feature_contracts.UIContributions
    assert HostUIContributions is feature_contracts.UIContributions
    assert get_type_hints(Feature.get_ui_contributions)["return"] == (
        feature_contracts.UIContributions | None
    )
    assert get_type_hints(HostFeature.get_ui_contributions)["return"] == (
        feature_contracts.UIContributions | None
    )


class _LegacyFeature(Feature):
    """Fixture using only the Feature contract that predates row 1."""

    async def initialize(self) -> None:
        pass

    @property
    def tool_description(self) -> str:
        return "legacy fixture"

    def get_permission_defaults(self):
        return "legacy permission API"

    def get_setup_steps(self):
        return ["legacy setup API"]


class _LegacyHostFeature(HostFeature):
    """Fixture using only the HostFeature contract that predates row 1."""

    async def on_host_start(self, ctx) -> None:
        ctx.append("started")

    async def on_host_stop(self, ctx) -> None:
        ctx.append("stopped")

    def get_permission_defaults(self):
        return "legacy host permission API"

    def get_setup_steps(self):
        return ["legacy host setup API"]


def test_existing_feature_fixture_works_without_new_overrides() -> None:
    agent = object()
    feature = _LegacyFeature(agent)

    assert feature.agent is agent
    assert feature.get_hooks() == []
    assert feature.get_router() is None
    assert feature.get_ui_contributions() is None
    assert feature.get_service_registrations() == ()
    assert feature.get_wait_provider_registrations() == ()
    assert feature.get_workflow_registrations() == ()
    assert feature.get_feature_permission_defaults() is None
    assert feature.get_setup_step_registrations() == ()
    assert feature.get_permission_defaults() == "legacy permission API"
    assert feature.get_setup_steps() == ["legacy setup API"]


@pytest.mark.asyncio
async def test_existing_host_feature_fixture_works_without_new_overrides() -> None:
    feature = _LegacyHostFeature()
    events: list[str] = []

    await feature.on_host_start(events)
    await feature.on_host_stop(events)

    assert events == ["started", "stopped"]
    assert feature.get_router() is None
    assert feature.get_ui_contributions() is None
    assert feature.get_service_registrations() == ()
    assert feature.get_wait_provider_registrations() == ()
    assert feature.get_workflow_registrations() == ()
    assert feature.get_feature_permission_defaults() is None
    assert feature.get_setup_step_registrations() == ()
    assert feature.get_permission_defaults() == "legacy host permission API"
    assert feature.get_setup_steps() == ["legacy host setup API"]
