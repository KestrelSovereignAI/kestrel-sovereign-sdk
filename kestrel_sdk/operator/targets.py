"""Browser-safe execution-target discovery and resolution contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from kestrel_sdk._validation import frozen_tokens, non_empty_text, stable_token
from .context import OperatorContext


@dataclass(frozen=True, slots=True)
class ExecutionTargetDescriptor:
    """Safe public description of a registered, opaque execution target.

    The deliberately closed shape contains only identifiers, a display label,
    and capability names. In particular there is no metadata escape hatch in
    which paths, commands, environment variables, credentials, or secrets can
    be placed.
    """

    target_id: str
    target_kind: str
    display_name: str
    tenant_id: str
    boundary_id: str
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        stable_token(self.target_id, "target_id")
        stable_token(self.target_kind, "target_kind")
        non_empty_text(self.display_name, "display_name")
        stable_token(self.tenant_id, "tenant_id")
        stable_token(self.boundary_id, "boundary_id")
        object.__setattr__(
            self,
            "capabilities",
            frozen_tokens(self.capabilities, "capabilities"),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the complete browser-safe wire shape."""

        return {
            "target_id": self.target_id,
            "target_kind": self.target_kind,
            "display_name": self.display_name,
            "tenant_id": self.tenant_id,
            "boundary_id": self.boundary_id,
            "capabilities": sorted(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class ExecutionTargetReference:
    """A caller's exact request for one target capability at one boundary."""

    target_id: str
    boundary_id: str
    capability: str

    def __post_init__(self) -> None:
        stable_token(self.target_id, "target_id")
        stable_token(self.boundary_id, "boundary_id")
        stable_token(self.capability, "capability")


@runtime_checkable
class ExecutionTargetResolver(Protocol):
    """Authorize and resolve registered targets to opaque runtime handles.

    Before returning, an implementation must require the supplied context to
    be fresh and authenticated, enforce its tenant against the registration's
    tenant, require the
    referenced boundary and capability, and ensure the exact target is both
    registered and entitled. It must fail closed and must not expose target
    configuration through either errors or the returned public descriptor.
    Concrete tenancy policy and engine adapters belong to the runtime.
    """

    async def resolve_execution_target(
        self,
        reference: ExecutionTargetReference,
        context: OperatorContext,
    ) -> object:
        """Return an authorized opaque handle or raise a runtime-owned error."""
        ...


__all__ = [
    "ExecutionTargetDescriptor",
    "ExecutionTargetReference",
    "ExecutionTargetResolver",
]
