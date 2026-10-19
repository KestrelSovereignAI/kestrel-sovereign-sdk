"""Provider-neutral environments, computer sessions and visual action records.

The host owns authority, policy and durable custody. A model's declared intent
is not an approval. Inference routes are intentionally absent from this module.
"""

from __future__ import annotations

from datetime import UTC
import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, SecretBytes, SecretStr, model_validator

from kestrel_sdk.resources import (
    Identifier, NonNegativeInt, PositiveInt, ResourceContract, ResourceScope, USD,
)

ENVIRONMENT_PROVIDER_ENTRY_POINT_GROUP = "kestrel_sovereign.environment_providers"
COMPUTER_DRIVER_ENTRY_POINT_GROUP = "kestrel_sovereign.computer_drivers"

Location = Literal["local", "remote"]
ComputerCapability = Literal[
    "screenshot", "click", "type", "key", "scroll", "wait", "navigate",
    "accessibility", "dom", "clipboard", "files", "exec", "takeover",
]
ImageDigest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
PinnedImage = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$"),
]


class EnvironmentCapabilities(ResourceContract):
    """A provider/driver's actual capabilities, not a cloud-brand promise."""

    provider_id: Identifier
    location: Location
    surfaces: frozenset[Literal["browser", "desktop"]]
    operations: frozenset[ComputerCapability]
    profile_persistence: bool = False
    cpu_only: bool = True


class EnvironmentRequest(ResourceContract):
    request_id: Identifier
    scope: ResourceScope
    provider_id: Identifier
    profile_id: Identifier
    image: PinnedImage
    surface: Literal["browser", "desktop"]
    required_capabilities: frozenset[ComputerCapability]
    location: Location
    requested_at: AwareDatetime
    expires_at: AwareDatetime
    idle_ttl_seconds: PositiveInt
    ready_timeout_seconds: PositiveInt
    max_hourly_cost_usd: USD
    max_total_cost_usd: USD
    region: Identifier | None = None

    @model_validator(mode="after")
    def bounded_lifetime(self) -> Self:
        lifetime = (self.expires_at - self.requested_at).total_seconds()
        if lifetime <= 0:
            raise ValueError("environment expiry must follow requested_at")
        if max(self.idle_ttl_seconds, self.ready_timeout_seconds) > lifetime:
            raise ValueError("idle/readiness timeout exceeds environment lifetime")
        if "screenshot" not in self.required_capabilities:
            raise ValueError("visual computer use requires screenshot capability")
        return self

    def digest(self) -> str:
        """Bind a quote to exact constraints, independent of set/timezone order."""

        payload = self.model_dump(mode="json")
        payload["required_capabilities"] = sorted(self.required_capabilities)
        for name in ("requested_at", "expires_at"):
            payload[name] = getattr(self, name).astimezone(UTC).isoformat()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class EnvironmentQuote(ResourceContract):
    """An offer bound to an exact request; acceptance is host policy, not a flag."""

    quote_id: Identifier
    request_id: Identifier
    request_sha256: ImageDigest
    scope: ResourceScope
    provider_id: Identifier
    location: Location
    region: Identifier | None = None
    expires_at: AwareDatetime
    hourly_cost_usd: USD
    estimated_total_cost_usd: USD
    capabilities: EnvironmentCapabilities

    @model_validator(mode="after")
    def same_provider(self) -> Self:
        if (self.provider_id, self.location) != (
            self.capabilities.provider_id, self.capabilities.location
        ):
            raise ValueError("quote and capabilities identify different providers")
        return self


class EnvironmentLease(ResourceContract):
    """Durable custody; failed/expired is not proof that billing has stopped."""

    lease_id: Identifier
    request_id: Identifier
    scope: ResourceScope
    provider_id: Identifier
    location: Location
    state: Literal["pending", "ready", "unknown", "failed", "releasing", "released"]
    expires_at: AwareDatetime
    # Azure ARM paths and AWS ARNs are opaque provider IDs, not SDK tokens.
    provider_resource_id: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    released_at: AwareDatetime | None = None
    actual_cost_usd: USD | None = None

    @model_validator(mode="after")
    def physical_release(self) -> Self:
        if (self.state == "released") != (self.released_at is not None):
            raise ValueError("released requires confirmed physical release time")
        if self.state == "ready" and self.provider_resource_id is None:
            raise ValueError("ready requires an owned provider resource")
        return self


class DriverConnection(ResourceContract):
    """Host-only connection. Dumping this record never emits routes/credentials."""

    lease_id: Identifier
    driver_id: Identifier
    endpoint: SecretStr = Field(exclude=True, repr=False)
    credential: SecretStr = Field(exclude=True, repr=False)
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def nonempty_connection(self) -> Self:
        if not self.endpoint.get_secret_value() or not self.credential.get_secret_value():
            raise ValueError("driver endpoint and credential must be non-empty")
        return self


class SessionRequest(ResourceContract):
    """Host-admitted identity/limits; the driver returns its actual session state."""

    session_id: Identifier
    scope: ResourceScope
    lease_id: Identifier
    surface: Literal["browser", "desktop"]
    required_capabilities: frozenset[ComputerCapability]
    expires_at: AwareDatetime


class ComputerSession(ResourceContract):
    session_id: Identifier
    scope: ResourceScope
    lease_id: Identifier
    driver_id: Identifier
    surface: Literal["browser", "desktop"]
    capabilities: frozenset[ComputerCapability]
    state: Literal["ready", "paused", "unknown", "closing", "closed"]
    controller: Literal["agent", "human", "none"]
    control_epoch: NonNegativeInt
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def controller_state(self) -> Self:
        if self.state == "ready" and self.controller != "agent":
            raise ValueError("ready sessions require the agent controller")
        if self.state != "ready" and self.controller == "agent":
            raise ValueError("agent input is fenced outside ready state")
        if self.controller == "human" and self.state != "paused":
            raise ValueError("human control requires paused state")
        return self


class ScreenFrame(ResourceContract):
    observation_id: Identifier
    session_id: Identifier
    control_epoch: NonNegativeInt
    observed_at: AwareDatetime
    width: PositiveInt
    height: PositiveInt
    image_sha256: ImageDigest
    media_type: Literal["image/png", "image/jpeg"]


class ScreenObservation(ResourceContract):
    """Private bytes for an authenticated consumer, separate from frame metadata.

Observation delivery is a privacy decision for the host. Generic JSON/repr
deliberately exclude pixels; an authorized transport accesses image explicitly.
"""

    frame: ScreenFrame
    image: SecretBytes = Field(exclude=True, repr=False)

    @model_validator(mode="after")
    def verified_payload(self) -> Self:
        payload = self.image.get_secret_value()
        if not payload or len(payload) > 32 * 1024 * 1024:
            raise ValueError("observation image must contain at most 32 MiB")
        if hashlib.sha256(payload).hexdigest() != self.frame.image_sha256:
            raise ValueError("observation image does not match the frame digest")
        return self


class Click(ResourceContract):
    kind: Literal["click"] = "click"
    x: NonNegativeInt
    y: NonNegativeInt
    button: Literal["left", "middle", "right"] = "left"


class TypeText(ResourceContract):
    kind: Literal["type"] = "type"
    text: Annotated[str, Field(min_length=1, max_length=16384, repr=False)]


class Key(ResourceContract):
    kind: Literal["key"] = "key"
    key: Annotated[str, Field(min_length=1, max_length=64)]


class Scroll(ResourceContract):
    kind: Literal["scroll"] = "scroll"
    delta_x: Annotated[int, Field(ge=-10000, le=10000)] = 0
    delta_y: Annotated[int, Field(ge=-10000, le=10000)] = 0


class Wait(ResourceContract):
    kind: Literal["wait"] = "wait"
    milliseconds: Annotated[int, Field(ge=0, le=10000)]


class Navigate(ResourceContract):
    """Untrusted URL; the driver and network boundary must enforce host policy."""

    kind: Literal["navigate"] = "navigate"
    url: Annotated[str, Field(min_length=1, max_length=4096, repr=False)]


ComputerAction = Annotated[
    Click | TypeText | Key | Scroll | Wait | Navigate, Field(discriminator="kind")
]


class ActionRequest(ResourceContract):
    """An action is bound to an observation and controller generation.

The action payload is private content: serialize only into an authorized,
encrypted transport/store. Do not log a model_dump of this record.
"""

    action_id: Identifier
    session_id: Identifier
    observation_id: Identifier
    control_epoch: NonNegativeInt
    action: ComputerAction = Field(repr=False)


class ActionReceipt(ResourceContract):
    action_id: Identifier
    session_id: Identifier
    observation_id: Identifier
    control_epoch: NonNegativeInt
    outcome: Literal["completed", "denied", "unknown"]
    recorded_at: AwareDatetime
    reason_code: Identifier | None = None


class ControlRequest(ResourceContract):
    request_id: Identifier
    session_id: Identifier
    expected_control_epoch: NonNegativeInt
    operation: Literal["pause", "takeover", "resume", "close"]
