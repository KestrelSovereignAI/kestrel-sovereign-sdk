"""Optional authenticated human control; records are facts, never authority.

The host authenticates the consumer and holds current privacy/grant admission.
The driver independently enforces its installed lease and current state at the
effect boundary. The validation helpers below cannot replace either check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import AwareDatetime, Field, model_validator

from kestrel_sdk.resources import (
    Identifier, NonNegativeInt, PositiveInt, ResourceContract, ResourceScope,
)
from .models import (
    ActionReceipt, ActionRequest, Click, ComputerSession, ImageDigest,
    ScreenObservation,
)

HumanCapability = Literal["screenshot", "click", "type", "key", "scroll", "wait", "navigate"]


def _aware(at: datetime) -> None:
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("a trusted timezone-aware current time is required")


class HumanControlRequest(ResourceContract):
    """Fixed consumer lease requested only after an acknowledged takeover.

``consumer_id`` is resolved by the host from authenticated operator identity;
neither it nor any other field authenticates the holder. One consumer may be
installed per epoch. A new consumer requires another takeover epoch, even if
the previous lease was revoked. No implicit replacement or renewal is allowed.
"""

    control_id: Identifier
    scope: ResourceScope
    session_id: Identifier
    lease_id: Identifier
    driver_id: Identifier
    consumer_id: Identifier
    control_epoch: NonNegativeInt
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    capabilities: frozenset[HumanCapability]
    max_frame_age_seconds: PositiveInt

    @model_validator(mode="after")
    def bounded_request(self) -> Self:
        lifetime = (self.expires_at - self.issued_at).total_seconds()
        if lifetime <= 0 or self.max_frame_age_seconds > lifetime:
            raise ValueError("human frame/lifetime bounds are inconsistent")
        if "screenshot" not in self.capabilities:
            raise ValueError("human control requires screenshot capability")
        return self

    def digest(self) -> str:
        """Canonical correlation digest, including consumer and every limit."""
        payload = self.model_dump(mode="json")
        payload["capabilities"] = sorted(self.capabilities)
        for name in ("issued_at", "expires_at"):
            payload[name] = getattr(self, name).astimezone(UTC).isoformat()
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def require_session(self, session: ComputerSession, *, at: datetime) -> None:
        """Validate live snapshot facts; not current installed lease or access."""
        _aware(at)
        if (self.scope, self.session_id, self.lease_id, self.driver_id, self.control_epoch) != (
                session.scope, session.session_id, session.lease_id, session.driver_id, session.control_epoch):
            raise ValueError("human lease belongs to another owner/session/epoch")
        if session.state != "paused" or session.controller != "human":
            raise ValueError("human control requires a paused human session")
        if "takeover" not in session.capabilities or not self.capabilities <= session.capabilities:
            raise ValueError("human capabilities exceed the admitted session")
        if not self.issued_at <= at < self.expires_at <= session.expires_at:
            raise ValueError("human lease is outside its fixed lifetime")


class HumanControlLease(ResourceContract):
    """Original accepted binding; expiry is not proof of input retirement.

``revoked`` requires the driver to fence and drain/reconcile accepted input.
Timeouts retain the original lease and receipts as ``unknown`` or ``revoking``.
These records do not establish that such physical retirement actually occurred.
"""

    request: HumanControlRequest
    request_sha256: ImageDigest
    state: Literal["active", "revoking", "revoked", "unknown"]
    accepted_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def correlated_acceptance(self) -> Self:
        if self.request_sha256 != self.request.digest():
            raise ValueError("human lease does not match the original request digest")
        if not self.request.issued_at <= self.accepted_at < self.request.expires_at:
            raise ValueError("human lease acceptance is outside the requested lifetime")
        if (self.state == "revoked") != (self.revoked_at is not None):
            raise ValueError("revoked requires confirmed input retirement time")
        if self.revoked_at is not None and self.revoked_at < self.accepted_at:
            raise ValueError("input retirement precedes acceptance")
        return self

    def require_request(self, request: HumanControlRequest) -> None:
        if request.digest() != self.request_sha256:
            raise ValueError("human control request differs from the accepted binding")

    def require_active(self, session: ComputerSession, *, at: datetime) -> None:
        self.request.require_session(session, at=at)
        if self.state != "active" or at < self.accepted_at:
            raise ValueError("human lease is not active")


class HumanScreenObservation(ResourceContract):
    """Private frame in the installed consumer's distinct observation namespace."""

    control_id: Identifier
    consumer_id: Identifier
    observation: ScreenObservation

    def require_lease(self, lease: HumanControlLease, session: ComputerSession, *, at: datetime) -> None:
        lease.require_active(session, at=at)
        request, frame = lease.request, self.observation.frame
        if (self.control_id, self.consumer_id, frame.session_id, frame.control_epoch) != (
                request.control_id, request.consumer_id, request.session_id, request.control_epoch):
            raise ValueError("human observation belongs to another consumer/session/epoch")
        if not lease.accepted_at <= frame.observed_at <= at:
            raise ValueError("human observation is outside the accepted lease")
        if at - frame.observed_at >= timedelta(seconds=request.max_frame_age_seconds):
            raise ValueError("human observation is stale")


class HumanActionRequest(ResourceContract):
    """Human input with canonical payload, separate from the agent act channel.

The digest includes private typed text/URLs. Serialize only into an approved
encrypted transport/store; never log this request or its model dump.
"""

    control_id: Identifier
    consumer_id: Identifier
    action: ActionRequest = Field(repr=False)

    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        payload["action"]["not_after"] = self.action.not_after.astimezone(UTC).isoformat()
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def require_lease(
        self, lease: HumanControlLease, session: ComputerSession,
        observation: HumanScreenObservation, *, at: datetime,
    ) -> None:
        observation.require_lease(lease, session, at=at)
        request, action, frame = lease.request, self.action, observation.observation.frame
        if (self.control_id, self.consumer_id) != (request.control_id, request.consumer_id):
            raise ValueError("human action belongs to another consumer lease")
        if (action.session_id, action.control_epoch, action.observation_id) != (
                request.session_id, request.control_epoch, frame.observation_id):
            raise ValueError("human action belongs to another observation/session/epoch")
        if action.action.kind not in request.capabilities:
            raise ValueError("human action exceeds the consumer capabilities")
        if not (at < action.not_after <= min(request.expires_at, session.expires_at)
                and action.not_after - frame.observed_at <= timedelta(seconds=request.max_frame_age_seconds)):
            raise ValueError("human action deadline exceeds current frame/lease bounds")
        if isinstance(action.action, Click) and (
                action.action.x >= frame.width or action.action.y >= frame.height):
            raise ValueError("human click exceeds the observed screen")


class HumanActionReceipt(ResourceContract):
    """Reconciliation of original input, never permission to replay unknown work."""

    control_id: Identifier
    consumer_id: Identifier
    receipt: ActionReceipt

    def require_request(self, request: HumanActionRequest) -> None:
        receipt, action = self.receipt, request.action
        if (self.control_id, self.consumer_id, receipt.action_id, receipt.session_id,
                receipt.observation_id, receipt.control_epoch) != (
                request.control_id, request.consumer_id, action.action_id, action.session_id,
                action.observation_id, action.control_epoch):
            raise ValueError("human receipt belongs to another original action")


class HumanControlRevocation(ResourceContract):
    """Idempotent retirement targeting the original binding, including its epoch."""

    request_id: Identifier
    control_id: Identifier
    session_id: Identifier
    consumer_id: Identifier
    expected_control_epoch: NonNegativeInt

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()

    def require_target(self, lease: HumanControlLease, session: ComputerSession) -> None:
        """Allow original-owner retirement after expiry/epoch change, no data access."""
        request = lease.request
        if (request.scope, request.session_id, request.lease_id, request.driver_id) != (
                session.scope, session.session_id, session.lease_id, session.driver_id):
            raise ValueError("human retirement belongs to another owner/session")
        if (self.control_id, self.session_id, self.consumer_id, self.expected_control_epoch) != (
                request.control_id, request.session_id, request.consumer_id, request.control_epoch):
            raise ValueError("human retirement differs from the original consumer lease")


@runtime_checkable
class HumanControl(Protocol):
    """Optional driver-owned authenticated transport, with no new public endpoint.

The host resolves the exact human consumer from current authentication and
holds original privacy/grants through each delivery/effect. Request records,
consumer IDs, causation and prior receipts grant no authority. Transport must
bind the authenticated consumer to the installed lease; do not expose CDP or
reuse ComputerDriver.act/observe as a human control shim.

open requires current paused/human state, takeover capability and a capability
subset. It durably binds the original request digest before acceptance. Same
ID/digest reconciles, changed retries conflict; revoked/expired IDs never reopen.
Only one consumer lease may be installed per control epoch; replacing it needs
a fresh takeover epoch. No implicit renewal or reconnect resumes input.

All data/effects require the exact live installed consumer lease, session,
resource lease, epoch, expiry and current host admission at execution/delivery,
not merely validated snapshots. Human observation IDs use a namespace distinct
from agent and other consumers. Recheck latest frame/display/focus, action
support, coordinates and deadline immediately before effect. Persist input
intent and its digest before input; original action IDs are deduplicated across
agent and human channels. Changed ID bindings conflict. Unknown outcomes retain
original receipt/lease/input custody and reconcile; never automatically replay.

status/action_status require current metadata-read authority and may reconcile
expired/revoked bindings without exposing pixels or accepting input. No visible
record raises ResourceNotFound; absence is not proof an in-flight open/action
cannot finish. Safety revoke retains original authenticated ownership, targets
the original epoch even after expiry, and fences/drains accepted work before
acknowledging revoked. A timeout remains unknown, never retirement evidence.
Priority Stop/takeover/close/release revoke and drain through an independent
control path, without waiting for ordinary observation/input admission. Resume
revokes every human lease, drains/reconciles input, advances the epoch and
revalidates display before agent observation/input resumes. An uncertain input
prevents handback until reconciled or the original process is retired.
"""

    async def open(self, session: ComputerSession, request: HumanControlRequest) -> HumanControlLease: ...
    async def status(self, session: ComputerSession, request: HumanControlRequest) -> HumanControlLease: ...
    async def observe(self, session: ComputerSession, lease: HumanControlLease) -> HumanScreenObservation: ...
    async def act(
        self, session: ComputerSession, lease: HumanControlLease, request: HumanActionRequest,
    ) -> HumanActionReceipt: ...
    async def action_status(
        self, session: ComputerSession, lease: HumanControlLease, action_id: str,
    ) -> HumanActionReceipt: ...
    async def revoke(self, session: ComputerSession, request: HumanControlRevocation) -> HumanControlLease: ...


@runtime_checkable
class HumanControlProvider(Protocol):
    """Negotiate alongside takeover capability; absent either, refuse human input."""

    def human_control(self) -> HumanControl: ...
