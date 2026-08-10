"""Contract tests for external feature-owned declarative contributions."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from enum import Enum
from pathlib import Path

import pytest

from kestrel_sdk.features import (
    ContributionContractError,
    Feature,
    FeaturePermissionDefaults,
    PermissionLevel,
    SetupFlow,
    SetupStepClassification,
    SetupStepContext,
    SetupStepRegistration,
    UIContributions,
    WaitProviderRegistration,
    WorkflowRegistration,
    await_contribution_result,
    normalize_setup_flow,
    order_setup_step_registrations,
    validate_feature_contributions,
)
from kestrel_sdk.operator import (
    ServiceDescriptor,
    ServiceRegistration,
    ServiceScope,
)
from kestrel_sdk.signals import (
    RedactionPolicy,
    SignalMode,
    SourceRegistration,
)
from kestrel_sdk.tools import Outcome, WaitStatus, Waitable


class _Agent:
    id = "agent-fixture"


class _Router:
    routes = ("/fixture/runs",)


class _Waitable:
    kind = "fixture-run"
    signal = "fixture.run.complete"

    async def poll(self, handle: str) -> WaitStatus:
        return WaitStatus(Outcome.DONE, f"{handle} complete")


async def _source_handler(payload: dict) -> dict:
    return payload


async def _workflow_actor(run_id: str) -> str:
    return run_id


def _setup_step(ctx: SetupStepContext) -> None:
    ctx.record("fixture configured")


_SOURCE = SourceRegistration(
    name="fixture.workflow.source",
    schema=dict,
    default_mode=SignalMode.ACTION,
    allowed_modes=frozenset({SignalMode.ACTION}),
    handler=_source_handler,
    log_redaction=RedactionPolicy(lambda payload: "fixture payload"),
)

_SECOND_SOURCE = SourceRegistration(
    name="fixture.workflow.second-source",
    schema=dict,
    default_mode=SignalMode.ACTION,
    allowed_modes=frozenset({SignalMode.ACTION}),
    handler=_source_handler,
    log_redaction=RedactionPolicy(lambda payload: "fixture payload"),
)


class ExternalFixtureFeature(Feature):
    """Out-of-tree-style fixture importing no Sovereign implementation."""

    def __init__(self, agent: _Agent) -> None:
        super().__init__(agent)
        self.name = "fixture-feature"
        provider = _Waitable()
        self._services = (
            ServiceRegistration(
                ServiceDescriptor(
                    "fixture.operator", "1.0.0", ServiceScope.AGENT
                ),
                self,
                self.owner,
                agent_id=self.agent.id,
            ),
        )
        self._wait_providers = (
            WaitProviderRegistration(self.owner, provider.kind, provider),
        )
        self._workflows = (
            WorkflowRegistration(
                self.owner,
                "fixture.workflow",
                _workflow_actor,
                (_SOURCE, _SECOND_SOURCE),
            ),
        )
        self._permission_defaults = FeaturePermissionDefaults(
            tool_overrides={
                "fixture_status": PermissionLevel.ALLOW,
                "fixture_launch": PermissionLevel.ALWAYS_ASK,
            }
        )
        self._setup_steps = (
            SetupStepRegistration(
                owner=self.owner,
                name="fixture",
                step=_setup_step,
                classification=SetupStepClassification.OPTIONAL,
                order=700,
                after=("integrations",),
            ),
        )

    async def initialize(self) -> None:
        pass

    @property
    def tool_description(self) -> str:
        return "External contract fixture"

    def get_service_registrations(self):
        return self._services

    def get_wait_provider_registrations(self):
        return self._wait_providers

    def get_workflow_registrations(self):
        return self._workflows

    def get_feature_permission_defaults(self):
        return self._permission_defaults

    def get_setup_step_registrations(self):
        return self._setup_steps

    def get_router(self):
        return _Router()

    def get_ui_contributions(self):
        return UIContributions(
            static_dir="/fixture/static",
            modules=["fixture-panel.js"],
            capability="fixture.operate",
        )


def test_external_feature_can_expose_every_row_one_seam_via_sdk() -> None:
    feature = ExternalFixtureFeature(_Agent())

    service = feature.get_service_registrations()[0]
    wait = feature.get_wait_provider_registrations()[0]
    workflow = feature.get_workflow_registrations()[0]
    setup = feature.get_setup_step_registrations()[0]
    permissions = feature.get_feature_permission_defaults()

    assert service.reference.agent_id == "agent-fixture"
    assert service.owner == feature.owner
    assert service.identity == (
        feature.owner,
        "fixture.operator",
        "1.0.0",
        "agent",
        "agent-fixture",
    )
    assert isinstance(wait.provider, Waitable)
    assert wait.identity == (feature.owner, "fixture-run")
    assert workflow.identity == (feature.owner, "fixture.workflow")
    assert workflow.actor is _workflow_actor
    assert workflow.sources == (_SOURCE, _SECOND_SOURCE)
    assert setup.identity == (feature.owner, "fixture")
    assert setup.name == "fixture"
    assert setup.after == ("integrations",)
    assert permissions is not None
    assert permissions.feature_default is PermissionLevel.ASK
    assert permissions.tool_overrides["fixture_status"] is PermissionLevel.ALLOW
    assert feature.get_router().routes == ("/fixture/runs",)
    assert feature.get_ui_contributions().modules == ["fixture-panel.js"]


def test_contribution_methods_return_instance_stable_objects() -> None:
    feature = ExternalFixtureFeature(_Agent())

    assert feature.get_service_registrations() is feature.get_service_registrations()
    assert (
        feature.get_wait_provider_registrations()
        is feature.get_wait_provider_registrations()
    )
    assert (
        feature.get_workflow_registrations()
        is feature.get_workflow_registrations()
    )
    assert (
        feature.get_feature_permission_defaults()
        is feature.get_feature_permission_defaults()
    )
    assert (
        feature.get_setup_step_registrations()
        is feature.get_setup_step_registrations()
    )


def test_owned_identity_supports_exact_deterministic_teardown() -> None:
    feature = ExternalFixtureFeature(_Agent())
    owned = {
        registration.identity: registration
        for registration in (
            *feature.get_wait_provider_registrations(),
            *feature.get_workflow_registrations(),
        )
    }
    other_owner = WorkflowRegistration(
        "another-feature", "fixture.workflow", _workflow_actor, (_SOURCE,)
    )
    owned[other_owner.identity] = other_owner

    for identity in tuple(owned):
        if identity[0] == feature.owner:
            del owned[identity]

    assert owned == {other_owner.identity: other_owner}


def test_permission_vocabulary_is_closed_conservative_and_immutable() -> None:
    assert {level.value for level in PermissionLevel} == {
        "allow",
        "auto",
        "deny",
        "always_ask",
        "ask",
        "session",
    }
    source = {"tool": "deny"}
    defaults = FeaturePermissionDefaults(
        tool_overrides=source  # type: ignore[arg-type]
    )
    source["tool"] = "allow"

    assert defaults.feature_default is PermissionLevel.ASK
    assert defaults.tool_overrides == {"tool": PermissionLevel.DENY}
    with pytest.raises(TypeError):
        defaults.tool_overrides["tool"] = PermissionLevel.ALLOW  # type: ignore[index]
    with pytest.raises(ValueError):
        FeaturePermissionDefaults(
            feature_default="always_allow"  # type: ignore[arg-type]
        )


def test_registration_validation_prevents_ambiguous_identity() -> None:
    with pytest.raises(ValueError, match="match provider.kind"):
        WaitProviderRegistration("fixture", "different", _Waitable())
    with pytest.raises(ValueError, match="stable token"):
        WorkflowRegistration("not an owner", "actor", _workflow_actor, (_SOURCE,))
    with pytest.raises(TypeError, match="actor must be callable"):
        WorkflowRegistration(
            "fixture", "actor", object(), _SOURCE  # type: ignore[arg-type]
        )


def test_workflow_registration_supports_empty_and_plural_sources() -> None:
    empty = WorkflowRegistration("fixture", "empty", _workflow_actor)
    plural = WorkflowRegistration(
        "fixture", "plural", _workflow_actor, [_SOURCE, _SECOND_SOURCE]  # type: ignore[arg-type]
    )

    assert empty.sources == ()
    assert plural.sources == (_SOURCE, _SECOND_SOURCE)
    assert plural.identity == ("fixture", "plural")
    actors = {plural.identity: plural.actor}
    sources = {source.name: source for source in plural.sources}
    assert actors == {("fixture", "plural"): _workflow_actor}
    assert set(sources) == {_SOURCE.name, _SECOND_SOURCE.name}
    with pytest.raises(ValueError, match="source names must be unique"):
        WorkflowRegistration(
            "fixture", "duplicate", _workflow_actor, (_SOURCE, _SOURCE)
        )


def test_setup_flow_normalizes_real_non_string_enum() -> None:
    class SovereignFlow(Enum):
        SETUP = "setup"
        CHECK = "check"

    assert normalize_setup_flow(SetupFlow.SETUP) is SetupFlow.SETUP
    assert normalize_setup_flow("check") is SetupFlow.CHECK
    assert normalize_setup_flow(SovereignFlow.SETUP) is SetupFlow.SETUP
    with pytest.raises(TypeError, match="string-valued Enum"):
        normalize_setup_flow(Enum("BadFlow", {"BAD": 1}).BAD)


def test_setup_registration_is_frozen_and_context_is_structural() -> None:
    class Context:
        project_dir = Path("/tmp/project")
        agent_data_root = Path("/tmp/project/agents")
        flow = SetupFlow.CHECK
        prompter = object()

        def __init__(self) -> None:
            self.changes: list[str] = []
            self.blockers: list[str] = []

        def record(self, message: str) -> None:
            self.changes.append(message)

        def block(self, message: str) -> None:
            self.blockers.append(message)

    registration = SetupStepRegistration("fixture", "fixture", _setup_step)
    context = Context()

    assert isinstance(context, SetupStepContext)
    registration.step(context)
    assert context.changes == ["fixture configured"]
    assert registration.classification is SetupStepClassification.OPTIONAL
    with pytest.raises(FrozenInstanceError):
        registration.order = 5  # type: ignore[misc]


def test_setup_ordering_enforces_constraints_and_deterministic_ties() -> None:
    steps = (
        SetupStepRegistration("feature-b", "beta", _setup_step, order=10),
        SetupStepRegistration("feature-a", "alpha", _setup_step, order=10),
        SetupStepRegistration(
            "feature-c", "last", _setup_step, order=0, after=("beta",)
        ),
    )

    assert [step.name for step in order_setup_step_registrations(steps)] == [
        "alpha",
        "beta",
        "last",
    ]
    with pytest.raises(ContributionContractError, match="unknown step"):
        order_setup_step_registrations(
            (SetupStepRegistration("feature", "step", _setup_step, after=("missing",)),)
        )
    with pytest.raises(ContributionContractError, match="cycle"):
        order_setup_step_registrations(
            (
                SetupStepRegistration("feature", "one", _setup_step, after=("two",)),
                SetupStepRegistration("feature", "two", _setup_step, after=("one",)),
            )
        )


def test_runtime_validation_checks_types_owners_and_duplicate_identities() -> None:
    feature = ExternalFixtureFeature(_Agent())
    validated = validate_feature_contributions(
        feature.owner,
        tool_names=("fixture_status", "fixture_launch"),
        services=feature.get_service_registrations(),
        wait_providers=feature.get_wait_provider_registrations(),
        workflows=feature.get_workflow_registrations(),
        permission_defaults=feature.get_feature_permission_defaults(),
        setup_steps=feature.get_setup_step_registrations(),
    )

    assert validated.services is feature.get_service_registrations()
    assert validated.services[0].service is feature
    with pytest.raises(ContributionContractError, match="must be returned as a tuple"):
        validate_feature_contributions(
            feature.owner,
            tool_names=(),
            services=list(feature.get_service_registrations()),
            wait_providers=(),
            workflows=(),
            permission_defaults=None,
            setup_steps=(),
        )
    wrong_owner = SetupStepRegistration("other-feature", "step", _setup_step)
    with pytest.raises(ContributionContractError, match="expected 'fixture-feature'"):
        validate_feature_contributions(
            feature.owner,
            tool_names=(),
            services=(),
            wait_providers=(),
            workflows=(),
            permission_defaults=None,
            setup_steps=(wrong_owner,),
        )
    workflow = feature.get_workflow_registrations()[0]
    with pytest.raises(ContributionContractError, match="duplicate identity"):
        validate_feature_contributions(
            feature.owner,
            tool_names=(),
            services=(),
            wait_providers=(),
            workflows=(workflow, workflow),
            permission_defaults=None,
            setup_steps=(),
        )


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("services", (object(),)),
        ("wait_providers", (object(),)),
        ("workflows", (object(),)),
        ("setup_steps", (object(),)),
        ("permission_defaults", object()),
    ],
)
def test_runtime_validation_rejects_every_bad_contribution_type(
    field: str, bad_value: object
) -> None:
    values = {
        "tool_names": (),
        "services": (),
        "wait_providers": (),
        "workflows": (),
        "permission_defaults": None,
        "setup_steps": (),
    }
    values[field] = bad_value

    with pytest.raises(ContributionContractError):
        validate_feature_contributions("feature-owner", **values)


def test_validation_rejects_cross_workflow_source_name_collisions() -> None:
    workflows = (
        WorkflowRegistration("fixture", "one", _workflow_actor, (_SOURCE,)),
        WorkflowRegistration("fixture", "two", _workflow_actor, (_SOURCE,)),
    )

    with pytest.raises(ContributionContractError, match="source name"):
        validate_feature_contributions(
            "fixture",
            tool_names=(),
            services=(),
            wait_providers=(),
            workflows=workflows,
            permission_defaults=None,
            setup_steps=(),
        )


def test_validation_rejects_permission_overrides_for_unknown_tools() -> None:
    with pytest.raises(ContributionContractError, match="unknown feature tools"):
        validate_feature_contributions(
            "fixture",
            tool_names=("known",),
            services=(),
            wait_providers=(),
            workflows=(),
            permission_defaults=FeaturePermissionDefaults(
                tool_overrides={"unknown": PermissionLevel.ALLOW}
            ),
            setup_steps=(),
        )


@pytest.mark.asyncio
async def test_contribution_callables_may_be_sync_or_async_without_loss() -> None:
    async def async_value() -> str:
        return "async"

    assert await await_contribution_result("sync") == "sync"
    assert await await_contribution_result(async_value()) == "async"


def test_base_feature_defaults_preserve_existing_subclasses() -> None:
    class BareFeature(Feature):
        async def initialize(self) -> None:
            pass

        @property
        def tool_description(self) -> str:
            return "bare"

    feature = BareFeature(_Agent())
    assert feature.owner == "BareFeature"
    assert feature.get_service_registrations() == ()
    assert feature.get_wait_provider_registrations() == ()
    assert feature.get_workflow_registrations() == ()
    assert feature.get_feature_permission_defaults() is None
    assert feature.get_setup_step_registrations() == ()
