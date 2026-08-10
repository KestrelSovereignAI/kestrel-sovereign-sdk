"""Authenticated, fail-closed context for operator execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from kestrel_sdk._validation import frozen_tokens, stable_token


MAX_OPERATOR_CONTEXT_LIFETIME = timedelta(hours=1)


class OperatorAuthorizationError(PermissionError):
    """Raised when an operator context does not grant requested authority."""


@dataclass(frozen=True, slots=True)
class OperatorContext:
    """Immutable authorization facts established by the host runtime.

    Collection inputs are copied to ``frozenset`` instances. Checks use exact
    membership only: empty sets, unknown names, and wildcard-looking strings
    grant nothing implicitly.
    """

    principal_id: str
    tenant_id: str
    granted_actions: frozenset[str]
    granted_capabilities: frozenset[str]
    permitted_boundary_ids: frozenset[str]
    correlation_id: str
    issued_at: datetime
    expires_at: datetime
    acting_agent_id: str | None = None

    def __post_init__(self) -> None:
        stable_token(self.principal_id, "principal_id")
        stable_token(self.tenant_id, "tenant_id")
        stable_token(self.correlation_id, "correlation_id")
        if self.acting_agent_id is not None:
            stable_token(self.acting_agent_id, "acting_agent_id")
        _aware_datetime(self.issued_at, "issued_at")
        _aware_datetime(self.expires_at, "expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if self.expires_at - self.issued_at > MAX_OPERATOR_CONTEXT_LIFETIME:
            raise ValueError("operator context validity must not exceed one hour")
        object.__setattr__(
            self,
            "granted_actions",
            frozen_tokens(self.granted_actions, "granted_actions"),
        )
        object.__setattr__(
            self,
            "granted_capabilities",
            frozen_tokens(self.granted_capabilities, "granted_capabilities"),
        )
        object.__setattr__(
            self,
            "permitted_boundary_ids",
            frozen_tokens(self.permitted_boundary_ids, "permitted_boundary_ids"),
        )

    def allows_action(self, action: str) -> bool:
        """Return whether an exact, valid action name was granted."""

        return _contains_valid(self.granted_actions, action)

    def allows_capability(self, capability: str) -> bool:
        """Return whether an exact, valid capability name was granted."""

        return _contains_valid(self.granted_capabilities, capability)

    def allows_boundary(self, boundary_id: str) -> bool:
        """Return whether an exact, valid boundary ID was permitted."""

        return _contains_valid(self.permitted_boundary_ids, boundary_id)

    def matches_tenant(self, tenant_id: str) -> bool:
        """Return whether ``tenant_id`` exactly matches the trusted tenant."""

        try:
            stable_token(tenant_id, "tenant_id")
        except (TypeError, ValueError):
            return False
        return tenant_id == self.tenant_id

    def require_action(self, action: str) -> None:
        """Raise :class:`OperatorAuthorizationError` unless action is granted."""

        if not self.allows_action(action):
            raise OperatorAuthorizationError("operator action is not granted")

    def require_capability(self, capability: str) -> None:
        """Raise unless the capability is present in this context."""

        if not self.allows_capability(capability):
            raise OperatorAuthorizationError("operator capability is not granted")

    def require_boundary(self, boundary_id: str) -> None:
        """Raise unless the boundary is present in this context."""

        if not self.allows_boundary(boundary_id):
            raise OperatorAuthorizationError("operator boundary is not permitted")

    def require_tenant(self, tenant_id: str) -> None:
        """Raise unless ``tenant_id`` is the authenticated tenant."""

        if not self.matches_tenant(tenant_id):
            raise OperatorAuthorizationError("operator tenant does not match")

    def require_fresh(self, at: datetime | None = None) -> None:
        """Raise unless this context is valid at the supplied trusted clock."""

        instant = datetime.now(UTC) if at is None else _aware_datetime(at, "at")
        if instant < self.issued_at or instant >= self.expires_at:
            raise OperatorAuthorizationError("operator context is not fresh")


def _contains_valid(values: frozenset[str], candidate: str) -> bool:
    try:
        stable_token(candidate, "authorization value")
    except (TypeError, ValueError):
        return False
    return candidate in values


def _aware_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


__all__ = [
    "MAX_OPERATOR_CONTEXT_LIFETIME",
    "OperatorAuthorizationError",
    "OperatorContext",
]
