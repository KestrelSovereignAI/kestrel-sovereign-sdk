"""Optional authenticated human control; records are facts, never authority.

The host authenticates the consumer and holds current privacy/grant admission.
The driver independently enforces its installed lease and current state at the
effect boundary. The validation helpers below cannot replace either check.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Annotated, Literal, Protocol, Self, runtime_checkable

from pydantic import AfterValidator, AwareDatetime, ConfigDict, Field, model_validator

from kestrel_sdk._validation import canonical_utc_datetime, json_roundtrip_datetime
from kestrel_sdk.resources import (
    Identifier, NonNegativeInt, PositiveInt, ResourceContract, ResourceScope,
)
from .models import (
    ActionReceipt, ActionRequest, Click, ComputerSession, ImageDigest,
    ScreenFrame, ScreenObservation,
)

_WireAwareDatetime = Annotated[AwareDatetime, AfterValidator(json_roundtrip_datetime)]

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
    issued_at: _WireAwareDatetime
    expires_at: _WireAwareDatetime
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
            payload[name] = canonical_utc_datetime(getattr(self, name))
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
    accepted_at: _WireAwareDatetime
    revoked_at: _WireAwareDatetime | None = None

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
        if not request.digest() == self.request_sha256 == self.request.digest():
            raise ValueError("human control request differs from the accepted binding")

    def require_active(self, session: ComputerSession, *, at: datetime) -> None:
        self.require_request(self.request)
        self.request.require_session(session, at=at)
        if self.state != "active" or at < self.accepted_at:
            raise ValueError("human lease is not active")


class HumanScreenFrame(ResourceContract):
    """Pixel-free current human frame facts, suitable for bounded retention.

    This record is not proof that a display remains current or grants access.
    The driver must compare its installed lease and latest frame at the effect.
    """

    control_id: Identifier
    consumer_id: Identifier
    frame: ScreenFrame

    def require_lease(self, lease: HumanControlLease, session: ComputerSession, *, at: datetime) -> None:
        lease.require_active(session, at=at)
        request, frame = lease.request, self.frame
        if (self.control_id, self.consumer_id, frame.session_id, frame.control_epoch) != (
                request.control_id, request.consumer_id, request.session_id, request.control_epoch):
            raise ValueError("human observation belongs to another consumer/session/epoch")
        if not lease.accepted_at <= frame.observed_at <= at:
            raise ValueError("human observation is outside the accepted lease")
        if at - frame.observed_at >= timedelta(seconds=request.max_frame_age_seconds):
            raise ValueError("human observation is stale")


class HumanScreenObservation(ResourceContract):
    """Private pixels; retain only frame_record when input needs metadata."""

    control_id: Identifier
    consumer_id: Identifier
    observation: ScreenObservation

    @property
    def frame_record(self) -> HumanScreenFrame:
        return HumanScreenFrame(control_id=self.control_id, consumer_id=self.consumer_id,
                                frame=self.observation.frame)

    def require_lease(self, lease: HumanControlLease, session: ComputerSession, *, at: datetime) -> None:
        self.frame_record.require_lease(lease, session, at=at)


class HumanActionRequest(ResourceContract):
    """Human input with canonical payload, separate from the agent act channel.

    The request AND its unsalted content digest are private. A digest can expose
    low-entropy secrets through guessing; never log/expose it or the request.
    Serialize only into an approved encrypted transport/store. Direct validation
    suppresses field input details in error text; enclosing non-SDK models and
    TypeAdapters must also configure hide_input_in_errors=True. Invalid tags
    and unknown field names can still appear, so log exception classes only.
    Detailed errors()/json() APIs can still contain inputs and must not be logged. Durable receipt content
    authentication needs a host-keyed digest inside encrypted custody, not this
    public hash.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    control_id: Identifier
    consumer_id: Identifier
    action: ActionRequest = Field(repr=False)

    def digest(self) -> str:
        """Private content correlation; never log or expose this unsalted hash."""
        payload = self.model_dump(mode="json")
        payload["action"]["not_after"] = canonical_utc_datetime(self.action.not_after)
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def require_lease(
        self, lease: HumanControlLease, session: ComputerSession,
        frame: HumanScreenFrame, *, at: datetime,
    ) -> None:
        frame.require_lease(lease, session, at=at)
        request, action, frame = lease.request, self.action, frame.frame
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
    control_epoch: NonNegativeInt

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()

    def require_target(self, lease: HumanControlLease, session: ComputerSession) -> None:
        """Correlate original retirement after expiry/epoch change; grants are external."""
        lease.require_request(lease.request)
        request = lease.request
        if (request.scope, request.session_id, request.lease_id, request.driver_id) != (
                session.scope, session.session_id, session.lease_id, session.driver_id):
            raise ValueError("human retirement belongs to another owner/session")
        if (self.control_id, self.session_id, self.consumer_id, self.control_epoch) != (
                request.control_id, request.session_id, request.consumer_id, request.control_epoch):
            raise ValueError("human retirement differs from the original consumer lease")


class HumanControlRevocationTombstone(ResourceContract):
    """Confirmed durable pre-open fence; no accepted lease/input is implied.

    A worker may return this only after atomically establishing that no open
    has been accepted AND recording the target control ID as permanently fenced.
    An in-flight open must compare that fence in the same acceptance transaction.
    Unknown acceptance instead retains the original lease and its work custody.
    """

    request: HumanControlRevocation
    scope: ResourceScope
    lease_id: Identifier
    driver_id: Identifier
    fenced_at: _WireAwareDatetime

    def require_revocation(self, request: HumanControlRevocation, session: ComputerSession) -> None:
        """Correlate a pre-open fence; this does not prove actual durable storage."""
        if (self.scope, self.lease_id, self.driver_id, request.session_id) != (
                session.scope, session.lease_id, session.driver_id, session.session_id):
            raise ValueError("human pre-open fence differs from the original retirement target")
        if request != self.request:
            target = (request.control_id, request.session_id, request.consumer_id, request.control_epoch)
            original = (self.request.control_id, self.request.session_id,
                        self.request.consumer_id, self.request.control_epoch)
            if target == original:
                raise ValueError("human pre-open fence belongs to another revocation request ID")
            raise ValueError("human pre-open fence differs from the original retirement target")


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
    intent and its PRIVATE digest before input; original action IDs are
    deduplicated across agent and human channels. Changed ID bindings conflict. Unknown outcomes retain
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

    revoke racing open must durably fence its original control ID before it can
    return. If no open has been accepted, atomically record a pre-open tombstone
    and return HumanControlRevocationTombstone. Late open of that control ID
    conflicts, including changed consumer/payload retries. Absence of an open
    record is not enough: acceptance must compare the same durable fence.
    Repeating the same revocation request ID reconciles its original result.
    A new request ID for the same already-fenced target must atomically confirm
    the existing fence and return a tombstone bound to the new request, retaining
    the original fenced_at. It must not replace the fence or accept a changed
    target. Unknown acceptance never becomes a tombstone: retain the original
    lease/input retirement.
    status of a pre-open tombstoned control ID raises ResourceConflict, not
    ResourceNotFound; reconcile with the original revoke or a new revocation
    for that exact fenced target, preserving its original fenced_at.
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
    async def revoke(
        self, session: ComputerSession, request: HumanControlRevocation,
    ) -> HumanControlLease | HumanControlRevocationTombstone: ...


@runtime_checkable
class HumanControlProvider(Protocol):
    """Negotiate alongside takeover capability; absent either, refuse human input."""

    def human_control(self) -> HumanControl: ...
