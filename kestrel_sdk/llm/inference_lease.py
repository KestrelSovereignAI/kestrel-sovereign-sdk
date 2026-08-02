"""Provider-neutral contracts for privately leased inference capacity.

The SDK owns only the boundary.  Kestrel core selects a provider and owns LLM
routing; provider packages own their infrastructure lifecycle.  In particular,
this module deliberately has no Runpod, Vast, Vertex, or framework imports.

Route credentials are host-only values.  They use :class:`pydantic.SecretStr`
so repr/logging is redacted, and every public serializer omits the endpoint and
credentials entirely.  Agent-facing code must serialize leases with
``InferenceLease.to_public_dict()`` rather than dataclass helpers.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from itertools import pairwise
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import SecretStr

INFERENCE_LEASE_PROVIDER_ENTRY_POINT_GROUP = (
    "kestrel_sovereign.inference_lease_providers"
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SECRET_KEY_SEGMENTS = frozenset(
    {
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "password",
        "secret",
        "token",
    }
)
_SECRET_KEY_PAIRS = frozenset({("api", "key"), ("private", "key")})
_PUBLIC_SCALARS = (str, int, float, bool, type(None))


class InferenceLeaseState(str, Enum):
    """Provider-neutral lifecycle states for one inference allocation."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    RELEASING = "releasing"
    RELEASED = "released"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        """Return whether no more provider work may make this lease ready."""

        return self in {
            InferenceLeaseState.FAILED,
            InferenceLeaseState.RELEASED,
            InferenceLeaseState.EXPIRED,
        }


class InferencePrivacy(str, Enum):
    """Strongest network exposure a request permits."""

    PRIVATE_NETWORK = "private_network"
    AUTHENTICATED_ENDPOINT = "authenticated_endpoint"
    PUBLIC_ENDPOINT = "public_endpoint"


_PRIVACY_EXPOSURE = {
    InferencePrivacy.PRIVATE_NETWORK: 0,
    InferencePrivacy.AUTHENTICATED_ENDPOINT: 1,
    InferencePrivacy.PUBLIC_ENDPOINT: 2,
}


class InferenceLeaseError(Exception):
    """Base exception for the remote-inference lease boundary."""


class InferenceLeaseConstraintError(InferenceLeaseError, ValueError):
    """A request or provider quote violates a caller constraint."""


class InferenceLeaseNotFoundError(InferenceLeaseError):
    """The requested lease does not exist."""


class InferenceLeaseOwnershipError(InferenceLeaseError, PermissionError):
    """A caller attempted to access another owner's lease."""


class InferenceLeaseProviderUnavailableError(InferenceLeaseError):
    """No configured provider can satisfy a request."""


class InferenceLeaseProvisioningError(InferenceLeaseError):
    """A selected provider could not provision or reconcile capacity."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_identifier(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(
            f"{name} must be 1-256 characters using letters, digits, '.', '_', "
            "':', '/', or '-'"
        )
    return normalized


def _validate_aware_timestamp(name: str, value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _money(name: str, value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal")
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite non-negative amount")
    return value


def _public_key_segments(value: str) -> tuple[str, ...]:
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return tuple(
        segment.lower()
        for segment in re.split(r"[^A-Za-z0-9]+", camel_split)
        if segment
    )


def _is_secret_like_public_key(value: str) -> bool:
    segments = _public_key_segments(value)
    if any(segment in _SECRET_KEY_SEGMENTS for segment in segments):
        return True
    return any(pair in _SECRET_KEY_PAIRS for pair in pairwise(segments))


def _privacy_satisfies(
    delivered: InferencePrivacy,
    permitted: InferencePrivacy,
) -> bool:
    """Return whether delivered exposure is no weaker than permitted."""

    return _PRIVACY_EXPOSURE[delivered] <= _PRIVACY_EXPOSURE[permitted]


def _freeze_public_value(value: Any, *, path: str) -> Any:
    """Deep-freeze JSON-shaped public metadata and reject secret-like keys."""

    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise ValueError(f"{path} keys must be non-empty strings")
            if _is_secret_like_public_key(raw_key):
                raise ValueError(f"{path} cannot contain secret-like key {raw_key!r}")
            frozen[raw_key] = _freeze_public_value(
                item,
                path=f"{path}.{raw_key}",
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_public_value(item, path=f"{path}[]") for item in value)
    if not isinstance(value, _PUBLIC_SCALARS):
        raise TypeError(f"{path} must contain only JSON scalar/list/object values")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} cannot contain non-finite numbers")
    return value


def _freeze_public_metadata(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping")
    frozen = _freeze_public_value(value, path="metadata")
    assert isinstance(frozen, Mapping)
    return frozen


def _thaw_public_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_public_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_public_value(item) for item in value]
    return value


@dataclass(frozen=True)
class InferenceLeaseRequest:
    """Immutable caller constraints for one private inference session."""

    request_id: str
    owner_id: str = field(repr=False)
    model: str
    runtime: str
    max_hourly_cost_usd: Decimal
    max_total_cost_usd: Decimal
    privacy: InferencePrivacy = InferencePrivacy.AUTHENTICATED_ENDPOINT
    capabilities: tuple[str, ...] = ()
    allowed_regions: tuple[str, ...] = ()
    expected_concurrency: int = 1
    expected_session_seconds: int = 900
    idle_ttl_seconds: int = 300
    ready_deadline_seconds: int = 900
    requested_at: datetime = field(default_factory=_utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _validate_identifier("request_id", self.request_id)
        )
        object.__setattr__(
            self, "owner_id", _validate_identifier("owner_id", self.owner_id)
        )
        object.__setattr__(self, "model", _validate_identifier("model", self.model))
        object.__setattr__(
            self, "runtime", _validate_identifier("runtime", self.runtime).lower()
        )
        object.__setattr__(
            self,
            "max_hourly_cost_usd",
            _money("max_hourly_cost_usd", self.max_hourly_cost_usd),
        )
        object.__setattr__(
            self,
            "max_total_cost_usd",
            _money("max_total_cost_usd", self.max_total_cost_usd),
        )
        if not isinstance(self.privacy, InferencePrivacy):
            raise TypeError("privacy must be an InferencePrivacy")
        if isinstance(self.capabilities, str):
            raise TypeError("capabilities must be a sequence, not a string")
        capabilities = tuple(
            _validate_identifier("capability", value).lower()
            for value in self.capabilities
        )
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("capabilities cannot contain duplicates")
        object.__setattr__(self, "capabilities", capabilities)
        if isinstance(self.allowed_regions, str):
            raise TypeError("allowed_regions must be a sequence, not a string")
        regions = tuple(
            _validate_identifier("allowed_region", value).lower()
            for value in self.allowed_regions
        )
        if len(regions) != len(set(regions)):
            raise ValueError("allowed_regions cannot contain duplicates")
        object.__setattr__(self, "allowed_regions", regions)
        for name in (
            "expected_concurrency",
            "expected_session_seconds",
            "idle_ttl_seconds",
            "ready_deadline_seconds",
        ):
            object.__setattr__(self, name, _positive_int(name, getattr(self, name)))
        if self.idle_ttl_seconds > self.expected_session_seconds:
            raise ValueError("idle_ttl_seconds cannot exceed expected_session_seconds")
        object.__setattr__(
            self,
            "requested_at",
            _validate_aware_timestamp("requested_at", self.requested_at),
        )
        object.__setattr__(self, "metadata", _freeze_public_metadata(self.metadata))

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize caller constraints without the owner identifier."""

        return {
            "request_id": self.request_id,
            "model": self.model,
            "runtime": self.runtime,
            "max_hourly_cost_usd": str(self.max_hourly_cost_usd),
            "max_total_cost_usd": str(self.max_total_cost_usd),
            "privacy": self.privacy.value,
            "capabilities": list(self.capabilities),
            "allowed_regions": list(self.allowed_regions),
            "expected_concurrency": self.expected_concurrency,
            "expected_session_seconds": self.expected_session_seconds,
            "idle_ttl_seconds": self.idle_ttl_seconds,
            "ready_deadline_seconds": self.ready_deadline_seconds,
            "requested_at": self.requested_at.isoformat(),
            "metadata": _thaw_public_value(self.metadata),
        }


@dataclass(frozen=True)
class InferenceProviderCapability:
    """One deterministic matchable capability advertised by a provider."""

    runtime: str
    privacy: tuple[InferencePrivacy, ...]
    capabilities: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    max_concurrency: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "runtime", _validate_identifier("runtime", self.runtime).lower()
        )
        if isinstance(self.privacy, (str, InferencePrivacy)):
            raise TypeError("privacy must be a sequence of privacy modes")
        privacy = tuple(self.privacy)
        if any(not isinstance(item, InferencePrivacy) for item in privacy):
            raise TypeError("privacy entries must be InferencePrivacy values")
        if not privacy or len(privacy) != len(set(privacy)):
            raise ValueError("privacy must contain unique supported modes")
        object.__setattr__(self, "privacy", privacy)
        if isinstance(self.capabilities, str):
            raise TypeError("capabilities must be a sequence, not a string")
        capabilities = tuple(
            _validate_identifier("capability", value).lower()
            for value in self.capabilities
        )
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("capabilities cannot contain duplicates")
        object.__setattr__(self, "capabilities", capabilities)
        if isinstance(self.regions, str):
            raise TypeError("regions must be a sequence, not a string")
        regions = tuple(
            _validate_identifier("region", value).lower() for value in self.regions
        )
        if len(regions) != len(set(regions)):
            raise ValueError("regions cannot contain duplicates")
        object.__setattr__(self, "regions", regions)
        object.__setattr__(
            self,
            "max_concurrency",
            _positive_int("max_concurrency", self.max_concurrency),
        )

    def satisfies(self, request: InferenceLeaseRequest) -> bool:
        """Return whether this declaration can satisfy static constraints."""

        privacy_satisfied = any(
            _privacy_satisfies(provided, request.privacy) for provided in self.privacy
        )
        if self.runtime != request.runtime or not privacy_satisfied:
            return False
        if request.expected_concurrency > self.max_concurrency:
            return False
        if not set(request.capabilities).issubset(self.capabilities):
            return False
        if request.allowed_regions:
            return bool(set(request.allowed_regions).intersection(self.regions))
        return True


@dataclass(frozen=True)
class InferenceLeaseQuote:
    """Read-only, expiring provider quote produced before provisioning."""

    quote_id: str
    request_id: str
    provider_name: str
    runtime: str
    region: str
    privacy: InferencePrivacy
    hourly_cost_usd: Decimal
    estimated_total_cost_usd: Decimal
    estimated_ready_seconds: int
    expires_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("quote_id", "request_id", "provider_name", "runtime", "region"):
            value = _validate_identifier(name, getattr(self, name))
            object.__setattr__(
                self,
                name,
                value.lower()
                if name in {"provider_name", "runtime", "region"}
                else value,
            )
        if not isinstance(self.privacy, InferencePrivacy):
            raise TypeError("privacy must be an InferencePrivacy")
        object.__setattr__(
            self, "hourly_cost_usd", _money("hourly_cost_usd", self.hourly_cost_usd)
        )
        object.__setattr__(
            self,
            "estimated_total_cost_usd",
            _money("estimated_total_cost_usd", self.estimated_total_cost_usd),
        )
        object.__setattr__(
            self,
            "estimated_ready_seconds",
            _non_negative_int("estimated_ready_seconds", self.estimated_ready_seconds),
        )
        object.__setattr__(
            self, "expires_at", _validate_aware_timestamp("expires_at", self.expires_at)
        )
        object.__setattr__(self, "metadata", _freeze_public_metadata(self.metadata))

    def validate_for(
        self, request: InferenceLeaseRequest, *, now: datetime | None = None
    ) -> None:
        """Fail closed if this quote cannot be used for ``request``."""

        current = _validate_aware_timestamp("now", now or _utc_now())
        if self.request_id != request.request_id:
            raise InferenceLeaseConstraintError(
                "quote request_id does not match request"
            )
        if self.runtime != request.runtime:
            raise InferenceLeaseConstraintError("quote runtime does not match request")
        if not _privacy_satisfies(self.privacy, request.privacy):
            raise InferenceLeaseConstraintError(
                "quote privacy is weaker than requested"
            )
        if request.allowed_regions and self.region not in request.allowed_regions:
            raise InferenceLeaseConstraintError("quote region is not allowed")
        if self.hourly_cost_usd > request.max_hourly_cost_usd:
            raise InferenceLeaseConstraintError("quote exceeds maximum hourly cost")
        if self.estimated_total_cost_usd > request.max_total_cost_usd:
            raise InferenceLeaseConstraintError("quote exceeds maximum total cost")
        if self.estimated_ready_seconds > request.ready_deadline_seconds:
            raise InferenceLeaseConstraintError("quote exceeds readiness deadline")
        if self.expires_at <= current:
            raise InferenceLeaseConstraintError("quote is expired")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "quote_id": self.quote_id,
            "request_id": self.request_id,
            "provider_name": self.provider_name,
            "runtime": self.runtime,
            "region": self.region,
            "privacy": self.privacy.value,
            "hourly_cost_usd": str(self.hourly_cost_usd),
            "estimated_total_cost_usd": str(self.estimated_total_cost_usd),
            "estimated_ready_seconds": self.estimated_ready_seconds,
            "expires_at": self.expires_at.isoformat(),
            "metadata": _thaw_public_value(self.metadata),
        }


@dataclass(frozen=True, repr=False)
class InferenceRoute:
    """Host-only OpenAI-compatible route; never serialize this object directly."""

    endpoint: SecretStr
    model: str
    api_key: SecretStr | None = None
    secret_headers: Mapping[str, SecretStr] = field(default_factory=dict)
    context_window: int | None = None
    protocol: str = "openai"

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, SecretStr):
            raise TypeError("endpoint must be a SecretStr")
        endpoint = self.endpoint.get_secret_value()
        parts = urlsplit(endpoint)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "endpoint must be an http(s) URL without userinfo, query, or fragment"
            )
        object.__setattr__(self, "model", _validate_identifier("model", self.model))
        object.__setattr__(
            self, "protocol", _validate_identifier("protocol", self.protocol).lower()
        )
        if self.api_key is not None and not isinstance(self.api_key, SecretStr):
            raise TypeError("api_key must be a SecretStr when provided")
        if self.api_key is not None and not self.api_key.get_secret_value():
            raise ValueError("api_key cannot be empty when provided")
        if not isinstance(self.secret_headers, Mapping):
            raise TypeError("secret_headers must be a mapping")
        headers: dict[str, SecretStr] = {}
        for name, value in self.secret_headers.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("secret header names must be non-empty strings")
            if not isinstance(value, SecretStr):
                raise TypeError("secret header values must be SecretStr instances")
            secret_value = value
            if not secret_value.get_secret_value():
                raise ValueError("secret header values cannot be empty")
            headers[name] = secret_value
        object.__setattr__(self, "secret_headers", MappingProxyType(headers))
        if self.context_window is not None:
            object.__setattr__(
                self,
                "context_window",
                _positive_int("context_window", self.context_window),
            )

    def __repr__(self) -> str:
        api_key = "None" if self.api_key is None else "SecretStr('**********')"
        return (
            "InferenceRoute(endpoint=SecretStr('**********'), "
            f"model={self.model!r}, api_key={api_key}, "
            f"secret_headers=<redacted:{len(self.secret_headers)}>, "
            f"context_window={self.context_window!r}, protocol={self.protocol!r})"
        )

    def to_public_dict(self) -> dict[str, Any]:
        """Return only non-addressable route metadata."""

        return {
            "model": self.model,
            "protocol": self.protocol,
            "context_window": self.context_window,
            "authenticated": bool(self.api_key or self.secret_headers),
        }


@dataclass(frozen=True)
class InferenceLeaseFailure:
    """Sanitized provider failure suitable for an agent-facing response."""

    code: str
    message: str
    retryable: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "code", _validate_identifier("failure code", self.code).lower()
        )
        message = str(self.message).strip()
        if not message or len(message) > 1000:
            raise ValueError("failure message must contain 1-1000 characters")
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "metadata", _freeze_public_metadata(self.metadata))

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "metadata": _thaw_public_value(self.metadata),
        }


@dataclass(frozen=True)
class InferenceLease:
    """Provider state plus an optional host-only ready route."""

    lease_id: str
    quote_id: str
    request_id: str
    owner_id: str = field(repr=False)
    provider_name: str
    state: InferenceLeaseState
    model: str
    runtime: str
    privacy: InferencePrivacy
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    region: str | None = None
    hourly_cost_usd: Decimal | None = None
    estimated_total_cost_usd: Decimal | None = None
    route: InferenceRoute | None = field(default=None, repr=False)
    failure: InferenceLeaseFailure | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "lease_id",
            "quote_id",
            "request_id",
            "owner_id",
            "provider_name",
            "model",
            "runtime",
        ):
            value = _validate_identifier(name, getattr(self, name))
            object.__setattr__(
                self,
                name,
                value.lower() if name in {"provider_name", "runtime"} else value,
            )
        if not isinstance(self.state, InferenceLeaseState):
            raise TypeError("state must be an InferenceLeaseState")
        if not isinstance(self.privacy, InferencePrivacy):
            raise TypeError("privacy must be an InferencePrivacy")
        for name in ("created_at", "updated_at", "expires_at"):
            object.__setattr__(
                self,
                name,
                _validate_aware_timestamp(name, getattr(self, name)),
            )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must follow created_at")
        if self.region is not None:
            object.__setattr__(
                self, "region", _validate_identifier("region", self.region).lower()
            )
        for name in ("hourly_cost_usd", "estimated_total_cost_usd"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _money(name, value))
        if self.state is InferenceLeaseState.READY and self.route is None:
            raise ValueError("a ready lease requires a route")
        if self.route is not None and not isinstance(self.route, InferenceRoute):
            raise TypeError("route must be an InferenceRoute")
        if self.state is not InferenceLeaseState.READY and self.route is not None:
            raise ValueError("only a ready lease may carry a route")
        if self.route is not None and self.route.model != self.model:
            raise ValueError("route model must match lease model")
        if (
            self.state is InferenceLeaseState.READY
            and self.expires_at <= self.updated_at
        ):
            raise ValueError("a ready lease must not already be expired")
        if (
            self.state is InferenceLeaseState.EXPIRED
            and self.expires_at > self.updated_at
        ):
            raise ValueError("an expired lease must have reached its expiry")
        if self.state is InferenceLeaseState.FAILED and self.failure is None:
            raise ValueError("a failed lease requires a failure")
        if self.failure is not None and not isinstance(
            self.failure, InferenceLeaseFailure
        ):
            raise TypeError("failure must be an InferenceLeaseFailure")
        if self.state is not InferenceLeaseState.FAILED and self.failure is not None:
            raise ValueError("only a failed lease may carry a failure")
        object.__setattr__(self, "metadata", _freeze_public_metadata(self.metadata))

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    def assert_owner(self, owner_id: str) -> None:
        """Raise before returning any state to a non-owner."""

        if self.owner_id != owner_id:
            raise InferenceLeaseOwnershipError(
                "inference lease is owned by another agent"
            )

    def validate_for(
        self,
        request: InferenceLeaseRequest,
        quote: InferenceLeaseQuote,
    ) -> None:
        """Fail closed unless the realized lease honors request and quote."""

        if self.request_id != request.request_id or self.owner_id != request.owner_id:
            raise InferenceLeaseConstraintError("lease request or owner does not match")
        if self.quote_id != quote.quote_id:
            raise InferenceLeaseConstraintError("lease quote_id does not match quote")
        if self.provider_name != quote.provider_name:
            raise InferenceLeaseConstraintError("lease provider does not match quote")
        if self.model != request.model or self.runtime != request.runtime:
            raise InferenceLeaseConstraintError("lease model or runtime does not match")
        if not _privacy_satisfies(self.privacy, request.privacy):
            raise InferenceLeaseConstraintError(
                "lease privacy is weaker than requested"
            )
        if self.region is None or (
            request.allowed_regions and self.region not in request.allowed_regions
        ):
            raise InferenceLeaseConstraintError("lease region is not allowed")
        if (
            self.hourly_cost_usd is None
            or self.hourly_cost_usd > request.max_hourly_cost_usd
            or self.hourly_cost_usd > quote.hourly_cost_usd
        ):
            raise InferenceLeaseConstraintError(
                "lease exceeds quoted or maximum hourly cost"
            )
        if (
            self.estimated_total_cost_usd is None
            or self.estimated_total_cost_usd > request.max_total_cost_usd
            or self.estimated_total_cost_usd > quote.estimated_total_cost_usd
        ):
            raise InferenceLeaseConstraintError(
                "lease exceeds quoted or maximum total cost"
            )

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize without owner identity, endpoint, or credentials."""

        result: dict[str, Any] = {
            "lease_id": self.lease_id,
            "quote_id": self.quote_id,
            "request_id": self.request_id,
            "provider_name": self.provider_name,
            "state": self.state.value,
            "model": self.model,
            "runtime": self.runtime,
            "privacy": self.privacy.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "region": self.region,
            "hourly_cost_usd": (
                str(self.hourly_cost_usd) if self.hourly_cost_usd is not None else None
            ),
            "estimated_total_cost_usd": (
                str(self.estimated_total_cost_usd)
                if self.estimated_total_cost_usd is not None
                else None
            ),
            "route": self.route.to_public_dict() if self.route else None,
            "failure": self.failure.to_public_dict() if self.failure else None,
            "metadata": _thaw_public_value(self.metadata),
        }
        return result


@runtime_checkable
class InferenceLeaseProvider(Protocol):
    """Infrastructure-provider contract consumed by Kestrel core."""

    @property
    def provider_name(self) -> str:
        """Stable entry-point/provider identifier."""
        ...

    def capabilities(self) -> Sequence[InferenceProviderCapability]:
        """Return static capabilities without making billable mutations."""
        ...

    def is_available(self) -> bool:
        """Return whether credentials/configuration permit use."""
        ...

    async def quote(self, request: InferenceLeaseRequest) -> InferenceLeaseQuote:
        """Return a non-mutating, expiring quote for ``request``."""
        ...

    async def acquire(
        self,
        request: InferenceLeaseRequest,
        quote: InferenceLeaseQuote,
    ) -> InferenceLease:
        """Idempotently acquire or resume the request's provider lease."""
        ...

    async def status(self, owner_id: str, lease_id: str) -> InferenceLease:
        """Return current state after enforcing owner isolation."""
        ...

    async def release(self, owner_id: str, lease_id: str) -> InferenceLease:
        """Idempotently remove routing and release capacity for the owner."""
        ...


__all__ = [
    "INFERENCE_LEASE_PROVIDER_ENTRY_POINT_GROUP",
    "InferenceLease",
    "InferenceLeaseConstraintError",
    "InferenceLeaseError",
    "InferenceLeaseFailure",
    "InferenceLeaseNotFoundError",
    "InferenceLeaseOwnershipError",
    "InferenceLeaseProvider",
    "InferenceLeaseProviderUnavailableError",
    "InferenceLeaseProvisioningError",
    "InferenceLeaseQuote",
    "InferenceLeaseRequest",
    "InferenceLeaseState",
    "InferencePrivacy",
    "InferenceProviderCapability",
    "InferenceRoute",
]
