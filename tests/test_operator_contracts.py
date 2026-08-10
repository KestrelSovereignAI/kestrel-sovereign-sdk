"""Focused tests for feature-owned operator SDK contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from kestrel_sdk import OperatorContext
from kestrel_sdk.operator import (
    CapabilityDescriptor,
    ExecutionTargetDescriptor,
    ExecutionTargetReference,
    ExecutionTargetResolver,
    OperatorAuthorizationError,
    ServiceDescriptor,
    ServiceReference,
    ServiceRegistration,
    ServiceRequirement,
    ServiceResolver,
    ServiceScope,
)


def _context(**overrides: object) -> OperatorContext:
    values: dict[str, object] = {
        "principal_id": "principal-1",
        "tenant_id": "tenant-1",
        "granted_actions": {"operator.run"},
        "granted_capabilities": {"shell.execute"},
        "permitted_boundary_ids": {"workspace-1"},
        "correlation_id": "request-1",
        "issued_at": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        "expires_at": datetime(2026, 8, 10, 12, 15, tzinfo=UTC),
    }
    values.update(overrides)
    return OperatorContext(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("tenant_id", ["", "   ", None])
def test_operator_context_rejects_invalid_or_missing_tenant(tenant_id: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _context(tenant_id=tenant_id)


def test_operator_context_requires_tenant_field() -> None:
    with pytest.raises(TypeError):
        OperatorContext(  # type: ignore[call-arg]
            principal_id="principal-1",
            granted_actions=frozenset(),
            granted_capabilities=frozenset(),
            permitted_boundary_ids=frozenset(),
            correlation_id="request-1",
            issued_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
            expires_at=datetime(2026, 8, 10, 12, 15, tzinfo=UTC),
        )


def test_operator_context_normalizes_collections_and_is_frozen() -> None:
    actions = ["operator.run"]
    context = _context(granted_actions=actions)
    actions.append("operator.delete")

    assert context.granted_actions == frozenset({"operator.run"})
    assert not context.allows_action("operator.delete")
    with pytest.raises(FrozenInstanceError):
        context.tenant_id = "another"  # type: ignore[misc]


def test_operator_context_helpers_fail_closed_for_unknown_values() -> None:
    context = _context()

    assert context.allows_action("operator.run")
    assert context.allows_capability("shell.execute")
    assert context.allows_boundary("workspace-1")
    assert context.matches_tenant("tenant-1")
    assert not context.allows_action("operator.unknown")
    assert not context.allows_boundary("workspace-unknown")
    assert not context.allows_capability("")
    with pytest.raises(OperatorAuthorizationError):
        context.require_action("operator.unknown")
    with pytest.raises(OperatorAuthorizationError):
        context.require_boundary("workspace-unknown")
    with pytest.raises(OperatorAuthorizationError):
        context.require_tenant("tenant-2")


def test_operator_context_has_bounded_freshness_and_agent_attribution() -> None:
    context = _context(acting_agent_id="agent-7")
    context.require_fresh(datetime(2026, 8, 10, 12, 5, tzinfo=UTC))
    assert context.acting_agent_id == "agent-7"

    with pytest.raises(OperatorAuthorizationError, match="fresh"):
        context.require_fresh(datetime(2026, 8, 10, 12, 15, tzinfo=UTC))
    with pytest.raises(ValueError, match="one hour"):
        _context(
            expires_at=datetime(2026, 8, 10, 13, 0, tzinfo=UTC)
            + timedelta(microseconds=1)
        )
    with pytest.raises(ValueError, match="after"):
        _context(expires_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC))


def test_service_descriptors_are_versioned_and_scope_is_explicit() -> None:
    capability = CapabilityDescriptor("shell.execute", "1.0.0")
    descriptor = ServiceDescriptor(
        name="operator.shell",
        version="2.1.0",
        scope=ServiceScope.AGENT,
        capabilities=[capability],  # type: ignore[arg-type]
    )
    service = object()
    registration = ServiceRegistration(
        descriptor, service, "feature-owner", agent_id="agent-1"
    )

    assert descriptor.capabilities == (capability,)
    assert registration.reference == ServiceReference(
        "operator.shell", "2.1.0", ServiceScope.AGENT, "agent-1"
    )
    assert registration.service is service
    assert registration.owner == "feature-owner"
    assert registration.identity == (
        "feature-owner",
        "operator.shell",
        "2.1.0",
        "agent",
        "agent-1",
    )


def test_stable_service_requirement_uses_compatible_major_and_minimum() -> None:
    requirement = ServiceRequirement(
        "operator.shell", "2.1.0", ServiceScope.AGENT, "agent-1"
    )

    assert requirement.accepts(
        ServiceDescriptor("operator.shell", "2.1.0", ServiceScope.AGENT)
    )
    assert requirement.accepts(
        ServiceDescriptor("operator.shell", "2.9.3", ServiceScope.AGENT)
    )
    assert not requirement.accepts(
        ServiceDescriptor("operator.shell", "2.0.9", ServiceScope.AGENT)
    )
    assert not requirement.accepts(
        ServiceDescriptor("operator.shell", "3.0.0", ServiceScope.AGENT)
    )
    assert not requirement.accepts(
        ServiceDescriptor("operator.shell", "2.2.0-rc.1", ServiceScope.AGENT)
    )
    zero_requirement = ServiceRequirement(
        "operator.shell", "0.2.1", ServiceScope.AGENT, "agent-1"
    )
    assert zero_requirement.accepts(
        ServiceDescriptor("operator.shell", "0.2.9", ServiceScope.AGENT)
    )
    assert not zero_requirement.accepts(
        ServiceDescriptor("operator.shell", "0.3.0", ServiceScope.AGENT)
    )


@pytest.mark.parametrize("version", ["2.1.0-rc.1", "2.1.0+build.4"])
def test_service_requirement_rejects_non_stable_versions(version: str) -> None:
    with pytest.raises(ValueError):
        ServiceRequirement("operator.shell", version, ServiceScope.HOST)


@pytest.mark.parametrize("version", ["1.0.0+build.1", "2.4.1+sha.abcdef"])
def test_contract_identity_rejects_build_metadata(version: str) -> None:
    with pytest.raises(ValueError):
        ServiceDescriptor("operator.shell", version, ServiceScope.HOST)
    with pytest.raises(ValueError):
        ServiceReference("operator.shell", version, ServiceScope.HOST)


@pytest.mark.parametrize(
    "version", ["", "1", "v1", "1.0", "01.0.0", "1.0.0-01"]
)
def test_service_version_validation(version: str) -> None:
    with pytest.raises(ValueError):
        CapabilityDescriptor("shell.execute", version)


def test_service_scope_and_registration_validation() -> None:
    with pytest.raises(TypeError):
        ServiceDescriptor("operator.shell", "1.0.0", "agent")  # type: ignore[arg-type]

    agent_descriptor = ServiceDescriptor(
        "operator.shell", "1.0.0", ServiceScope.AGENT
    )
    host_descriptor = ServiceDescriptor(
        "operator.inventory", "1.0.0", ServiceScope.HOST
    )
    with pytest.raises(ValueError, match="required"):
        ServiceRegistration(agent_descriptor, object(), "feature-owner")
    with pytest.raises(ValueError, match="not allowed"):
        ServiceRegistration(
            host_descriptor, object(), "feature-owner", agent_id="agent-1"
        )
    with pytest.raises(ValueError, match="stable token"):
        ServiceRegistration(host_descriptor, object(), "not an owner")
    with pytest.raises(TypeError, match="CapabilityDescriptor"):
        ServiceDescriptor(
            "operator.invalid",
            "1.0.0",
            ServiceScope.HOST,
            capabilities=(["unhashable"],),  # type: ignore[arg-type]
        )


def test_target_descriptor_has_closed_browser_safe_shape() -> None:
    capabilities = ["shell.execute"]
    descriptor = ExecutionTargetDescriptor(
        target_id="target-opaque-1",
        target_kind="container",
        display_name="Build worker",
        tenant_id="tenant-1",
        boundary_id="workspace-1",
        capabilities=capabilities,  # type: ignore[arg-type]
    )
    capabilities.append("secrets.read")

    assert descriptor.to_dict() == {
        "target_id": "target-opaque-1",
        "target_kind": "container",
        "display_name": "Build worker",
        "tenant_id": "tenant-1",
        "boundary_id": "workspace-1",
        "capabilities": ["shell.execute"],
    }
    forbidden = {"path", "command", "environment", "credentials", "metadata"}
    assert forbidden.isdisjoint(descriptor.to_dict())


@pytest.mark.parametrize(
    "display_name",
    [
        "Build\u2028worker",
        "Build\u202eworker",
        "Build\u200bworker",
        "Build\u00a0worker",
    ],
)
def test_target_descriptor_rejects_browser_unsafe_text(display_name: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        ExecutionTargetDescriptor(
            target_id="target-1",
            target_kind="container",
            display_name=display_name,
            tenant_id="tenant-1",
            boundary_id="workspace-1",
            capabilities={"shell.execute"},
        )


class _FixtureServiceResolver:
    def resolve_service(self, reference: ServiceReference) -> object | None:
        return None

    def resolve_compatible_service(
        self, requirement: ServiceRequirement
    ) -> object | None:
        return None


class _FixtureTargetResolver:
    async def resolve_execution_target(
        self,
        reference: ExecutionTargetReference,
        context: OperatorContext,
    ) -> object:
        return object()


def test_resolver_protocols_accept_structural_runtime_fixtures() -> None:
    assert isinstance(_FixtureServiceResolver(), ServiceResolver)
    assert isinstance(_FixtureTargetResolver(), ExecutionTargetResolver)
