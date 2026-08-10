"""Versioned service discovery contracts for feature-owned operators.

The SDK deliberately provides no registry. Sovereign's feature registry can
implement :class:`ServiceResolver`, with registrations becoming visible and
unavailable according to that registry's existing feature lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from kestrel_sdk._validation import (
    semantic_version,
    semantic_version_parts,
    stable_token,
    unique_tuple,
)


class ServiceScope(str, Enum):
    """Lifecycle and lookup scope for a registered service."""

    HOST = "host"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Stable name and independently versioned capability contract."""

    name: str
    version: str

    def __post_init__(self) -> None:
        stable_token(self.name, "name")
        semantic_version(self.version)


@dataclass(frozen=True, slots=True)
class ServiceDescriptor:
    """Public identity and capabilities of one service contract."""

    name: str
    version: str
    scope: ServiceScope
    capabilities: tuple[CapabilityDescriptor, ...] = ()

    def __post_init__(self) -> None:
        stable_token(self.name, "name")
        semantic_version(self.version)
        if not isinstance(self.scope, ServiceScope):
            raise TypeError("scope must be a ServiceScope")
        if isinstance(self.capabilities, (str, bytes)):
            raise TypeError("capabilities must contain CapabilityDescriptor values")
        try:
            capabilities = tuple(self.capabilities)
        except TypeError as error:
            raise TypeError(
                "capabilities must contain CapabilityDescriptor values"
            ) from error
        if not all(isinstance(item, CapabilityDescriptor) for item in capabilities):
            raise TypeError("capabilities must contain CapabilityDescriptor values")
        capabilities = unique_tuple(capabilities, "capabilities")
        names = [item.name for item in capabilities]
        if len(set(names)) != len(names):
            raise ValueError("capability names must be unique within a service")
        object.__setattr__(self, "capabilities", capabilities)


@dataclass(frozen=True, slots=True)
class ServiceReference:
    """An exact, versioned lookup key for a service registration."""

    name: str
    version: str
    scope: ServiceScope
    agent_id: str | None = None

    def __post_init__(self) -> None:
        stable_token(self.name, "name")
        semantic_version(self.version)
        if not isinstance(self.scope, ServiceScope):
            raise TypeError("scope must be a ServiceScope")
        _validate_scope_agent(self.scope, self.agent_id)


@dataclass(frozen=True, slots=True)
class ServiceRequirement:
    """Stable compatible lookup for a minimum service contract version.

    Compatibility means the same SemVer major and a release tuple greater
    than or equal to ``minimum_version``. While the required major is zero, the
    candidate must also have the same minor because ``0.x`` minor releases may
    break compatibility. Requirements are deliberately stable: prerelease and
    build versions are rejected. A resolver must likewise never satisfy one
    with a prerelease registration.
    """

    name: str
    minimum_version: str
    scope: ServiceScope
    agent_id: str | None = None

    def __post_init__(self) -> None:
        stable_token(self.name, "name")
        semantic_version(
            self.minimum_version,
            "minimum_version",
            allow_prerelease=False,
        )
        if not isinstance(self.scope, ServiceScope):
            raise TypeError("scope must be a ServiceScope")
        _validate_scope_agent(self.scope, self.agent_id)

    def accepts(self, descriptor: ServiceDescriptor) -> bool:
        """Return whether a descriptor is a stable compatible candidate."""

        if not isinstance(descriptor, ServiceDescriptor):
            return False
        if descriptor.name != self.name or descriptor.scope is not self.scope:
            return False
        if "-" in descriptor.version:
            return False
        required = semantic_version_parts(self.minimum_version)
        candidate = semantic_version_parts(descriptor.version)
        same_compatibility_line = candidate[0] == required[0] and (
            required[0] != 0 or candidate[1] == required[1]
        )
        return same_compatibility_line and candidate >= required


@dataclass(frozen=True, slots=True)
class ServiceRegistration:
    """A lifecycle-owned service paired with its opaque implementation.

    Agent-scoped registrations require ``agent_id``. Host-scoped registrations
    reject one, preventing an accidental fallback between the two namespaces.
    ``owner`` is the contributing feature's registered lifecycle identity;
    Sovereign validates it before activation and retains this exact object for
    teardown. The SDK neither owns nor starts/stops ``service``.
    """

    descriptor: ServiceDescriptor
    service: object
    owner: str
    agent_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, ServiceDescriptor):
            raise TypeError("descriptor must be a ServiceDescriptor")
        if self.service is None:
            raise ValueError("service must not be None")
        stable_token(self.owner, "owner")
        _validate_scope_agent(self.descriptor.scope, self.agent_id)

    @property
    def identity(self) -> tuple[str, str, str, str, str | None]:
        """Exact lifecycle identity used for registration and teardown."""

        return (
            self.owner,
            self.descriptor.name,
            self.descriptor.version,
            self.descriptor.scope.value,
            self.agent_id,
        )

    @property
    def reference(self) -> ServiceReference:
        """Return the exact lookup key for this registration."""

        return ServiceReference(
            name=self.descriptor.name,
            version=self.descriptor.version,
            scope=self.descriptor.scope,
            agent_id=self.agent_id,
        )


def _validate_scope_agent(scope: ServiceScope, agent_id: str | None) -> None:
    if scope is ServiceScope.AGENT:
        if agent_id is None:
            raise ValueError("agent_id is required for agent-scoped services")
        stable_token(agent_id, "agent_id")
    elif agent_id is not None:
        raise ValueError("agent_id is not allowed for host-scoped services")


@runtime_checkable
class ServiceResolver(Protocol):
    """Read-only view of the runtime's active feature registrations.

    Implementations must not fall back between host and agent scope. A result
    is available only while its owning feature registration is active. A
    resolved object must not be cached beyond the immediate operation that
    requested it: consumers resolve again for later operations so feature
    lifecycle teardown cannot leave a stale service reference. This protocol
    does not prescribe or create a second service registry.
    """

    def resolve_service(self, reference: ServiceReference) -> object | None:
        """Return the active opaque service implementation, or ``None``."""
        ...

    def resolve_compatible_service(
        self, requirement: ServiceRequirement
    ) -> object | None:
        """Return an active stable compatible service, or ``None``."""
        ...


__all__ = [
    "CapabilityDescriptor",
    "ServiceDescriptor",
    "ServiceReference",
    "ServiceRegistration",
    "ServiceRequirement",
    "ServiceResolver",
    "ServiceScope",
]
