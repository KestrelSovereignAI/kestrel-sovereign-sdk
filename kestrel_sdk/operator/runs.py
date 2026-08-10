"""Immutable contracts for the feature-owned operator run plane.

These models describe durable identities and semantic state. They do not
provide persistence, transport, telemetry, or engine adapters; those remain
the responsibility of Workflows, Sovereign, and the owning feature.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from ipaddress import IPv6Address
from types import MappingProxyType
from typing import Protocol, TypeAlias, runtime_checkable
from urllib.parse import unquote, urlsplit

from kestrel_sdk._validation import (
    browser_safe_string,
    frozen_tokens,
    non_empty_text,
    stable_token,
)
from .context import OperatorAuthorizationError, OperatorContext
from .targets import ExecutionTargetReference


RUN_LAUNCH_ACTION = "run.launch"
RUN_READ_ACTION = "run.read"
RUN_ATTACH_ACTION = "run.attach"
RUN_PAUSE_ACTION = "run.pause"
RUN_RESUME_ACTION = "run.resume"
RUN_CANCEL_ACTION = "run.cancel"
RUN_RETRY_ACTION = "run.retry"
ARTIFACT_READ_ACTION = "artifact.read"


class RunConflictError(RuntimeError):
    """A replay, state precondition, or legal transition could not be honored.

    Runtimes raise this both for idempotency conflicts and for controls that
    are illegal in the current state or lose an optimistic-concurrency race.
    """


class RunNotFoundError(LookupError):
    """A run or artifact is absent or not visible to the authenticated tenant.

    Runtime errors must not distinguish absence from another tenant's resource,
    preventing a cross-tenant existence oracle.
    """


class RunSource(str, Enum):
    """The authenticated source that requested a run."""

    MANUAL = "manual"
    AGENT = "agent"


class RunState(str, Enum):
    """Authoritative, engine-neutral lifecycle state of a durable run."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def is_terminal(self) -> bool:
        """Return whether no further execution-state transition is expected."""

        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


class RunControlAction(str, Enum):
    """Independently governed control verbs for an existing run."""

    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    RETRY = "retry"

    @property
    def required_action(self) -> str:
        """Return the exact authorization action governing this control."""

        return _RUN_CONTROL_AUTHORIZATION_ACTIONS[self]


class ArtifactAuthorizationAction(str, Enum):
    """Compatibility enum for artifact authorization actions.

    New code should use the canonical :data:`ARTIFACT_READ_ACTION` string.
    """

    READ = ARTIFACT_READ_ACTION


_RUN_CONTROL_AUTHORIZATION_ACTIONS = {
    RunControlAction.PAUSE: RUN_PAUSE_ACTION,
    RunControlAction.RESUME: RUN_RESUME_ACTION,
    RunControlAction.CANCEL: RUN_CANCEL_ACTION,
    RunControlAction.RETRY: RUN_RETRY_ACTION,
}


@dataclass(frozen=True, slots=True)
class RunLaunch:
    """Idempotent caller input for creating one durable run.

    ``run_id`` remains the durable workflow identity across controls and
    retries. A retry creates new :class:`RunAttempt` identities under the
    existing run/stage; it does not reuse an attempt or mint a replacement run.
    Runtime-owned acceptance time and initial queued state live on
    :class:`RunRecord`, so replay comparison contains no caller clock fields.
    """

    run_id: str
    kind: str
    source: RunSource
    initiated_by: str
    tenant_id: str
    target: ExecutionTargetReference
    idempotency_key: str
    orchestrator: str | None = None

    def __post_init__(self) -> None:
        stable_token(self.run_id, "run_id")
        stable_token(self.kind, "kind")
        if not isinstance(self.source, RunSource):
            raise TypeError("source must be a RunSource")
        stable_token(self.initiated_by, "initiated_by")
        stable_token(self.tenant_id, "tenant_id")
        if not isinstance(self.target, ExecutionTargetReference):
            raise TypeError("target must be an ExecutionTargetReference")
        stable_token(self.idempotency_key, "idempotency_key")
        if self.source is RunSource.MANUAL:
            if self.orchestrator is not None:
                raise ValueError("a manual run must not specify an orchestrator")
        elif self.orchestrator is None:
            raise ValueError("an agent run must specify an orchestrator")
        else:
            stable_token(self.orchestrator, "orchestrator")

    @property
    def replay_identity(self) -> tuple[object, ...]:
        """Return semantic caller input used for exact idempotent replay."""

        return (
            self.run_id,
            self.kind,
            self.source,
            self.initiated_by,
            self.tenant_id,
            self.target,
            self.orchestrator,
        )

    def idempotency_scope(self, tenant_id: str) -> tuple[str, str, str]:
        """Return the trusted tenant/action/key namespace for this launch.

        ``tenant_id`` must come from the authenticated
        :class:`OperatorContext`, never from this caller-created launch.
        ``run_id`` is semantic replay input rather than part of the key scope,
        so reusing one key for a different run conflicts.
        """

        stable_token(tenant_id, "trusted tenant_id")
        return (tenant_id, RUN_LAUNCH_ACTION, self.idempotency_key)

    def authorize(
        self, context: OperatorContext, *, at: datetime | None = None
    ) -> None:
        """Validate trusted launch authority and source attribution."""

        if not isinstance(context, OperatorContext):
            raise TypeError("context must be an OperatorContext")
        context.require_fresh(at)
        context.require_action(RUN_LAUNCH_ACTION)
        context.require_tenant(self.tenant_id)
        context.require_boundary(self.target.boundary_id)
        context.require_capability(self.target.capability)
        if self.source is RunSource.MANUAL:
            if context.acting_agent_id is not None:
                raise OperatorAuthorizationError(
                    "agent-mediated runs must use agent source provenance"
                )
            if self.initiated_by != context.principal_id:
                raise OperatorAuthorizationError("manual run source does not match")
        elif (
            context.acting_agent_id is None
            or self.initiated_by != context.acting_agent_id
        ):
            raise OperatorAuthorizationError("agent run source does not match")


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Runtime-accepted launch facts and current authoritative run state.

    ``sequence`` starts at zero and increases monotonically on every durable
    state mutation. ``state_changed_at`` is the trusted time of the latest
    state transition and defaults to ``accepted_at`` for the initial record.
    """

    launch: RunLaunch
    state: RunState
    accepted_at: datetime
    state_changed_at: datetime | None = None
    sequence: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.launch, RunLaunch):
            raise TypeError("launch must be a RunLaunch")
        if not isinstance(self.state, RunState):
            raise TypeError("state must be a RunState")
        accepted_at = _aware_datetime(self.accepted_at, "accepted_at")
        state_changed_at = (
            accepted_at
            if self.state_changed_at is None
            else _aware_datetime(self.state_changed_at, "state_changed_at")
        )
        if state_changed_at < accepted_at:
            raise ValueError("state_changed_at must not precede accepted_at")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("sequence must be an integer")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        object.__setattr__(self, "state_changed_at", state_changed_at)

    @property
    def run_id(self) -> str:
        """Return the durable workflow identity."""

        return self.launch.run_id

    def authorize(
        self,
        context: OperatorContext,
        action: str,
        *,
        at: datetime | None = None,
    ) -> None:
        """Authorize an operation against this resolved durable run.

        Runtimes call this after lookup and before returning or mutating the
        record or any related history, attachment, or artifact. A tenant
        mismatch raises the same :class:`RunNotFoundError` used for
        tenant-scoped absence, making the no-existence-oracle rule safe even
        when a runtime resolves by globally unique ID. For a visible record,
        freshness, action, boundary, and capability denials remain
        :class:`OperatorAuthorizationError`.
        """

        if not isinstance(context, OperatorContext):
            raise TypeError("context must be an OperatorContext")
        stable_token(action, "action")
        if not context.matches_tenant(self.launch.tenant_id):
            raise RunNotFoundError("run not found")
        context.require_fresh(at)
        context.require_action(action)
        context.require_boundary(self.launch.target.boundary_id)
        context.require_capability(self.launch.target.capability)


@dataclass(frozen=True, slots=True)
class RunStage:
    """Engine-neutral identity and semantic state for a stage within a run."""

    run_id: str
    stage_id: str
    kind: str
    state: RunState = RunState.QUEUED

    def __post_init__(self) -> None:
        stable_token(self.run_id, "run_id")
        stable_token(self.stage_id, "stage_id")
        stable_token(self.kind, "kind")
        if not isinstance(self.state, RunState):
            raise TypeError("state must be a RunState")


@dataclass(frozen=True, slots=True)
class RunAttempt:
    """One immutable numbered attempt of a stage within a durable run.

    Attempt numbers increase monotonically per ``(run_id, stage_id)``. Every
    retry has a fresh ``attempt_id`` and number; idempotent replay of the same
    retry control returns the attempt already created for that control.
    """

    run_id: str
    stage_id: str
    attempt_id: str
    attempt: int

    def __post_init__(self) -> None:
        stable_token(self.run_id, "run_id")
        stable_token(self.stage_id, "stage_id")
        stable_token(self.attempt_id, "attempt_id")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise TypeError("attempt must be an integer")
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")


@dataclass(frozen=True, slots=True)
class ExternalEngineJobLink:
    """Typed correlation from a run attempt to an external engine job."""

    run_id: str
    stage_id: str
    attempt_id: str
    engine: str
    external_job_id: str

    def __post_init__(self) -> None:
        stable_token(self.run_id, "run_id")
        stable_token(self.stage_id, "stage_id")
        stable_token(self.attempt_id, "attempt_id")
        stable_token(self.engine, "engine")
        non_empty_text(self.external_job_id, "external_job_id")


@dataclass(frozen=True, slots=True)
class RunControl:
    """Idempotent request for one separately authorized run control action.

    Runtime idempotency scope is exactly ``(context.tenant_id, run_id,
    required_action, stage_id, idempotency_key)``. An exact replay returns the
    prior outcome; differing semantic input in that scope raises
    :class:`RunConflictError`. ``stage_id`` is present only for ``RETRY``.
    When ``expected_sequence`` is present, the runtime applies the control only
    if it matches the run's current :attr:`RunRecord.sequence`; otherwise it
    raises :class:`RunConflictError`. This compare-and-set precondition is
    semantic request input but is not part of the idempotency scope.
    """

    run_id: str
    action: RunControlAction
    idempotency_key: str
    stage_id: str | None = None
    expected_sequence: int | None = None

    def __post_init__(self) -> None:
        stable_token(self.run_id, "run_id")
        if not isinstance(self.action, RunControlAction):
            raise TypeError("action must be a RunControlAction")
        stable_token(self.idempotency_key, "idempotency_key")
        if self.action is RunControlAction.RETRY:
            if self.stage_id is None:
                raise ValueError("stage_id is required for retry")
            stable_token(self.stage_id, "stage_id")
        elif self.stage_id is not None:
            raise ValueError("stage_id is allowed only for retry")
        if self.expected_sequence is not None:
            if isinstance(self.expected_sequence, bool) or not isinstance(
                self.expected_sequence, int
            ):
                raise TypeError("expected_sequence must be an integer or None")
            if self.expected_sequence < 0:
                raise ValueError("expected_sequence must be non-negative")

    def idempotency_scope(
        self, tenant_id: str
    ) -> tuple[str, str, str, str | None, str]:
        """Return the trusted tenant/run/action/stage/key control namespace."""

        stable_token(tenant_id, "tenant_id")
        return (
            tenant_id,
            self.run_id,
            self.action.required_action,
            self.stage_id,
            self.idempotency_key,
        )

    def authorize(
        self, context: OperatorContext, *, at: datetime | None = None
    ) -> None:
        """Perform only the pre-resolution freshness and action check.

        This is not complete run authorization. ``RunService.apply_control``
        must call it before lookup, then resolve ``run_id`` in the trusted
        tenant scope so missing and cross-tenant IDs share
        :class:`RunNotFoundError`, and finally call
        ``record.authorize(context, control.action.required_action)`` on the
        resolved :class:`RunRecord` before reading or mutating run state.
        """

        if not isinstance(context, OperatorContext):
            raise TypeError("context must be an OperatorContext")
        context.require_fresh(at)
        context.require_action(self.action.required_action)


@dataclass(frozen=True, slots=True)
class RunQuery:
    """Bounded filters and opaque cursor for tenant-scoped run discovery."""

    limit: int = 50
    cursor: str | None = None
    states: frozenset[RunState] = frozenset()
    kinds: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= self.limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if self.cursor is not None:
            if not isinstance(self.cursor, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,1024}", self.cursor
            ):
                raise ValueError("cursor must be a bounded opaque URL-safe token")
        states = frozenset(self.states)
        if not all(isinstance(state, RunState) for state in states):
            raise TypeError("states must contain RunState values")
        object.__setattr__(self, "states", states)
        object.__setattr__(self, "kinds", frozen_tokens(self.kinds, "kinds"))


@dataclass(frozen=True, slots=True)
class RunPage:
    """One bounded page of authorized records with unique ``run_id`` values.

    ``records`` is stored as the exact normalized tuple and is limited to 100
    entries. Any repeated ``run_id`` is invalid, including an exact duplicate
    of the same record; pages never silently deduplicate runtime output.
    """

    records: tuple[RunRecord, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.records, (str, bytes)):
            raise TypeError("records must be an iterable of RunRecord values")
        try:
            normalized = tuple(self.records)
        except TypeError as error:
            raise TypeError(
                "records must be an iterable of RunRecord values"
            ) from error
        if len(normalized) > 100:
            raise ValueError("a run page must contain at most 100 records")
        if not all(isinstance(record, RunRecord) for record in normalized):
            raise TypeError("records must contain RunRecord values")
        run_ids: set[str] = set()
        for record in normalized:
            if record.run_id in run_ids:
                raise ValueError(
                    "records must not contain duplicate run_id values"
                )
            run_ids.add(record.run_id)
        object.__setattr__(self, "records", normalized)
        if self.next_cursor is not None:
            if not isinstance(self.next_cursor, str):
                raise TypeError("next_cursor must be a string or None")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", self.next_cursor):
                raise ValueError(
                    "next_cursor must be a bounded opaque URL-safe token"
                )


JSONScalar: TypeAlias = None | bool | int | float | str
ImmutableJSON: TypeAlias = JSONScalar | tuple["ImmutableJSON", ...] | Mapping[
    str, "ImmutableJSON"
]

_MEDIA_TYPE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
)
_CANONICAL_ARTIFACT_PATH = re.compile(
    r"^/authorized/artifacts/([A-Za-z0-9](?:[A-Za-z0-9._:@-]{0,254}[A-Za-z0-9])?)$"
)
_VALID_PERCENT_ESCAPE = re.compile(r"%(?:[0-9A-Fa-f]{2})")
_MAX_METADATA_DEPTH = 16
_MAX_METADATA_NODES = 1024
_MAX_METADATA_KEYS = 256
_MAX_METADATA_STRING = 4096
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_FLOAT_SIGNIFICANT_DIGITS = 15


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Authorized metadata for an artifact belonging to a durable run.

    The canonical same-origin form is exactly
    ``/authorized/artifacts/<artifact_id>``. The opaque ID is an authorization
    lookup key, not a filename or storage path; the endpoint must reauthorize
    every dereference. Carefully validated absolute HTTPS URLs are permitted
    only as time-bounded signed URLs and therefore must carry a non-empty query.
    Syntax validation is not trust: the runtime must restrict absolute origins
    to an explicit allowlist and render links with a safe browser policy (for
    example, no opener access and no credential/referrer leakage).
    """

    artifact_id: str
    run_id: str
    type: str
    label: str
    media_type: str
    href: str
    metadata: Mapping[str, ImmutableJSON]

    def __post_init__(self) -> None:
        stable_token(self.artifact_id, "artifact_id")
        stable_token(self.run_id, "run_id")
        stable_token(self.type, "type")
        non_empty_text(self.label, "label")
        if not isinstance(self.media_type, str) or not _MEDIA_TYPE.fullmatch(
            self.media_type
        ):
            raise ValueError("media_type must be a valid type/subtype token")
        _authorized_artifact_href(self.href, self.artifact_id)
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))

    def __hash__(self) -> int:
        """Hash this immutable value model, including nested metadata."""

        return hash(
            (
                self.artifact_id,
                self.run_id,
                self.type,
                self.label,
                self.media_type,
                self.href,
                _hashable_json(self.metadata),
            )
        )


@runtime_checkable
class RunService(Protocol):
    """Authorized asynchronous service for the durable operator run plane.

    ``launch_run`` calls :meth:`RunLaunch.authorize`. Every post-launch method
    resolves the record, then calls :meth:`RunRecord.authorize` with its exact
    action before returning or mutating the run or related state. That helper
    converts tenant mismatch to the same typed not-found result as
    tenant-scoped absence. Run reads use :data:`RUN_READ_ACTION`,
    attachments use :data:`RUN_ATTACH_ACTION`, controls use the relevant
    ``RUN_*_ACTION`` constant, and artifact retrieval uses
    :data:`ARTIFACT_READ_ACTION`. This rechecks freshness and the durable run's
    tenant, boundary, and capability on every call; authority is never inferred
    from possession of an opaque ID.

    Launch idempotency scope is ``(context.tenant_id, RUN_LAUNCH_ACTION,
    idempotency_key)``. Exact semantic replay returns the original record
    (including its original acceptance and state clocks). Reusing that key for
    different semantic input, including a different ``run_id``, conflicts; an
    existing ``(context.tenant_id, run_id)`` launched under another key also
    conflicts. Controls use the scope documented on :class:`RunControl`.
    A control's optional ``expected_sequence`` compare-and-set precondition is
    checked against current durable state for a new request and is excluded
    from its idempotency scope. Illegal controls and optimistic-concurrency
    races raise :class:`RunConflictError`. Every absent or cross-tenant run or
    artifact raises the same :class:`RunNotFoundError` without exposing another
    tenant's resource existence; other authorization denials retain their
    explicit :class:`OperatorAuthorizationError` type.

    Durable workflow state, never telemetry, supplies run and stage state.
    Every state mutation advances ``RunRecord.sequence`` monotonically and
    updates ``state_changed_at``. Retry preserves ``run_id`` and creates one new
    :class:`RunAttempt` for the selected ``stage_id``; an exact retry replay
    returns the attempt created by the original control instead of duplicating
    it.
    """

    async def launch_run(
        self, launch: RunLaunch, context: OperatorContext
    ) -> RunRecord:
        """Authorize and idempotently accept an initially queued run."""
        ...

    async def get_run(self, run_id: str, context: OperatorContext) -> RunRecord:
        """Return one authorized run or raise ``RunNotFoundError``."""
        ...

    async def list_runs(
        self, query: RunQuery, context: OperatorContext
    ) -> RunPage:
        """Return one bounded page of runs visible to the context tenant."""
        ...

    async def list_stages(
        self,
        run_id: str,
        context: OperatorContext,
        *,
        limit: int = 100,
    ) -> tuple[RunStage, ...]:
        """Return stages in ascending ``stage_id`` order.

        ``limit`` must be between 1 and 100. This bounded method has no cursor;
        when more stages exist, it returns only the first ``limit`` entries.
        """
        ...

    async def list_attempts(
        self,
        run_id: str,
        stage_id: str,
        context: OperatorContext,
        *,
        limit: int = 100,
    ) -> tuple[RunAttempt, ...]:
        """Return attempts in ascending attempt-number order.

        ``limit`` must be between 1 and 100. This bounded method has no cursor;
        when more attempts exist, it returns only the first ``limit`` entries.
        """
        ...

    async def apply_control(
        self, control: RunControl, context: OperatorContext
    ) -> RunRecord:
        """Authorize, tenant-resolve, record-authorize, and apply a control.

        Call :meth:`RunControl.authorize` first, resolve in trusted tenant
        scope with the no-oracle not-found behavior, then call
        ``record.authorize(context, control.action.required_action)`` before
        applying the idempotent compare-and-set transition.
        """
        ...

    async def attach_external_job(
        self, link: ExternalEngineJobLink, context: OperatorContext
    ) -> ExternalEngineJobLink:
        """Require :data:`RUN_ATTACH_ACTION` and attach typed correlation."""
        ...

    async def attach_artifact(
        self, artifact: ArtifactRecord, context: OperatorContext
    ) -> ArtifactRecord:
        """Require :data:`RUN_ATTACH_ACTION` and attach artifact metadata."""
        ...

    async def get_artifact(
        self, artifact_id: str, context: OperatorContext
    ) -> ArtifactRecord:
        """Retrieve metadata after :data:`ARTIFACT_READ_ACTION` authorization."""
        ...


def _aware_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _authorized_artifact_href(value: str, artifact_id: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("href must be a bounded authorized artifact URL")
    _ascii_url(value)

    canonical = _CANONICAL_ARTIFACT_PATH.fullmatch(value)
    if canonical is not None:
        if canonical.group(1) != artifact_id:
            raise ValueError("canonical artifact href must use artifact_id")
        return value
    if value.startswith("/"):
        raise ValueError("relative href must use the canonical artifact endpoint")

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("href must be a valid signed HTTPS URL") from error
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or not _safe_hostname(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith("/")
        or not parsed.query
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("absolute href must be a signed HTTPS URL")
    _validate_percent_encoding(parsed.path, "href path", path=True)
    _validate_percent_encoding(parsed.query, "href query", path=False)
    decoded_path = parsed.path
    for _ in range(4):
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path
    else:
        raise ValueError("href contains excessive path encoding")
    if (
        "\\" in decoded_path
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
        or any(
            ord(character) < 33 or ord(character) > 126
            for character in decoded_path
        )
    ):
        raise ValueError("href contains an unsafe path")
    return value


def _ascii_url(value: str) -> None:
    if "\\" in value or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        raise ValueError("href contains unsafe characters")


def _safe_hostname(value: str) -> bool:
    if len(value) > 253 or "%" in value:
        return False
    if ":" in value:
        try:
            IPv6Address(value)
        except ValueError:
            return False
        return True
    hostname = value[:-1] if value.endswith(".") else value
    labels = hostname.split(".")
    return bool(hostname) and all(
        1 <= len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in label)
        for label in labels
    )


def _validate_percent_encoding(value: str, field_name: str, *, path: bool) -> None:
    index = 0
    while index < len(value):
        if value[index] == "%":
            match = _VALID_PERCENT_ESCAPE.match(value, index)
            if match is None:
                raise ValueError(f"{field_name} contains invalid encoding")
            decoded = chr(int(value[index + 1 : index + 3], 16))
            if (
                (path and decoded in "/\\.%")
                or ord(decoded) < 33
                or ord(decoded) > 126
            ):
                raise ValueError(f"{field_name} contains unsafe encoding")
            index += 3
        else:
            index += 1


@dataclass(slots=True)
class _MetadataBudget:
    nodes: int = 0
    keys: int = 0


def _freeze_metadata(value: Mapping[str, object]) -> Mapping[str, ImmutableJSON]:
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping")
    budget = _MetadataBudget(nodes=1)
    frozen: dict[str, ImmutableJSON] = {}
    for key, item in value.items():
        _metadata_key(key, budget)
        frozen[key] = _freeze_json(item, depth=1, budget=budget)
    return MappingProxyType(frozen)


def _hashable_json(value: ImmutableJSON) -> object:
    """Return an equality-consistent hashable representation of frozen JSON."""

    if isinstance(value, Mapping):
        return frozenset(
            (key, _hashable_json(item)) for key, item in value.items()
        )
    if isinstance(value, tuple):
        return tuple(_hashable_json(item) for item in value)
    return value


def _freeze_json(
    value: object, *, depth: int, budget: _MetadataBudget
) -> ImmutableJSON:
    if depth > _MAX_METADATA_DEPTH:
        raise ValueError(f"metadata must not exceed {_MAX_METADATA_DEPTH} levels")
    budget.nodes += 1
    if budget.nodes > _MAX_METADATA_NODES:
        raise ValueError(f"metadata must not exceed {_MAX_METADATA_NODES} nodes")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return browser_safe_string(
            value, "metadata string", max_length=_MAX_METADATA_STRING
        )
    if isinstance(value, int):
        if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise ValueError("metadata integer exceeds the interoperable JSON range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > _MAX_SAFE_INTEGER:
            raise ValueError("metadata number exceeds the interoperable JSON range")
        digits = len(Decimal(str(value)).as_tuple().digits)
        if digits > _MAX_FLOAT_SIGNIFICANT_DIGITS:
            raise ValueError("metadata number exceeds supported precision")
        return value
    if isinstance(value, Mapping):
        nested: dict[str, ImmutableJSON] = {}
        for key, item in value.items():
            _metadata_key(key, budget)
            nested[key] = _freeze_json(item, depth=depth + 1, budget=budget)
        return MappingProxyType(nested)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, depth=depth + 1, budget=budget) for item in value
        )
    raise TypeError("metadata values must be JSON-like")


def _metadata_key(value: object, budget: _MetadataBudget) -> None:
    if not isinstance(value, str):
        raise TypeError("metadata keys must be strings")
    browser_safe_string(value, "metadata key", max_length=256, allow_empty=False)
    budget.keys += 1
    if budget.keys > _MAX_METADATA_KEYS:
        raise ValueError(f"metadata must not exceed {_MAX_METADATA_KEYS} keys")


__all__ = [
    "ARTIFACT_READ_ACTION",
    "ArtifactAuthorizationAction",
    "ArtifactRecord",
    "ExternalEngineJobLink",
    "ImmutableJSON",
    "JSONScalar",
    "RUN_ATTACH_ACTION",
    "RUN_CANCEL_ACTION",
    "RUN_LAUNCH_ACTION",
    "RUN_PAUSE_ACTION",
    "RUN_READ_ACTION",
    "RUN_RESUME_ACTION",
    "RUN_RETRY_ACTION",
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
]
