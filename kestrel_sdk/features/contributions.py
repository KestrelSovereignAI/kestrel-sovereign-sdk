"""Declarative feature contribution contracts.

These data-only contracts let external features describe integration seams
without importing Sovereign's registries, setup wizard, or permission store.
The host runtime owns registration and teardown; the SDK only carries stable
identity and the implementation object to register.
"""

from __future__ import annotations

import heapq
import inspect
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Awaitable,
    Callable,
    Mapping,
    Protocol,
    TypeAlias,
    TypeVar,
    runtime_checkable,
)

from kestrel_sdk._validation import frozen_tokens, stable_token, unique_tuple
from kestrel_sdk.operator.discovery import ServiceRegistration
from kestrel_sdk.signals import SourceRegistration
from kestrel_sdk.tools import Waitable


class PermissionLevel(StrEnum):
    """Sovereign's complete feature/tool permission vocabulary.

    These defaults are evaluated only after non-overridable authentication,
    capability, tenancy, privacy, and policy denials. At the permission layer:

    * ``DENY`` rejects the invocation without prompting.
    * ``ALWAYS_ASK`` requires a fresh operator decision for every invocation;
      an earlier approval is never reused.
    * ``ASK`` requires an operator decision unless a durable decision already
      exists at the runtime's normal permission scope.
    * ``SESSION`` requires an operator decision once and may reuse an approval
      only for the current authenticated session.
    * ``ALLOW`` executes without prompting because an applicable explicit
      permission grants it.
    * ``AUTO`` executes without prompting under Sovereign's automatic-policy
      path and remains subject to every higher-priority enforcement gate.

    Sovereign must maintain a parity test that compares its complete enum and
    enforcement branches with these six values before accepting SDK permission
    contributions. Unknown values and incomplete mappings fail closed.
    """

    ALLOW = "allow"
    AUTO = "auto"
    DENY = "deny"
    ALWAYS_ASK = "always_ask"
    ASK = "ask"
    SESSION = "session"


@dataclass(frozen=True, slots=True)
class FeaturePermissionDefaults:
    """A feature's declarative permission defaults.

    ``ASK`` is deliberately conservative when a feature contributes this
    descriptor without selecting a different feature-wide default. Tool
    overrides are immutable and contain only the same closed vocabulary.
    This contract does not read or write the operator's permission store.
    """

    feature_default: PermissionLevel = PermissionLevel.ASK
    tool_overrides: Mapping[str, PermissionLevel] = field(default_factory=dict)

    def __post_init__(self) -> None:
        default = _permission_level(self.feature_default, "feature_default")
        if not isinstance(self.tool_overrides, Mapping):
            raise TypeError("tool_overrides must be a mapping")
        overrides: dict[str, PermissionLevel] = {}
        for tool_name, level in self.tool_overrides.items():
            stable_token(tool_name, "tool name")
            overrides[tool_name] = _permission_level(
                level, f"tool_overrides[{tool_name!r}]"
            )
        object.__setattr__(self, "feature_default", default)
        object.__setattr__(self, "tool_overrides", MappingProxyType(overrides))


def _permission_level(value: object, field_name: str) -> PermissionLevel:
    if isinstance(value, PermissionLevel):
        return value
    allowed = ", ".join(level.value for level in PermissionLevel)
    if isinstance(value, str):
        try:
            return PermissionLevel(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be one of: {allowed}") from exc
    raise ValueError(f"{field_name} must be one of: {allowed}")


class ContributionContractError(TypeError):
    """A feature returned contributions that violate the SDK contract."""


@dataclass(frozen=True, slots=True)
class WaitProviderRegistration:
    """A lifecycle owner and its exact wait-provider registration."""

    owner: str
    name: str
    provider: Waitable

    def __post_init__(self) -> None:
        _owned_identity(self.owner, self.name)
        if not isinstance(self.provider, Waitable):
            raise TypeError("provider must implement kestrel_sdk.tools.Waitable")
        if ":" in self.name:
            raise ValueError("wait provider name must not contain ':'")
        if self.provider.kind != self.name:
            raise ValueError("wait provider name must match provider.kind")

    @property
    def identity(self) -> tuple[str, str]:
        """Stable ``(owner, name)`` key used for deterministic teardown."""

        return (self.owner, self.name)


ContributionResult: TypeAlias = object | Awaitable[object]
WorkflowActor: TypeAlias = Callable[..., ContributionResult]


@dataclass(frozen=True, slots=True)
class WorkflowRegistration:
    """One lifecycle-owned workflow actor and zero or more signal sources.

    The actor remains a generic callable because its invocation convention is
    owned by the row-2 workflow runtime. Sources deliberately reuse the existing
    SDK :class:`SourceRegistration` boundary. The runtime registers ``actor``
    exactly once under :attr:`identity`, then independently registers every
    item in :attr:`sources`; it must not duplicate the actor per source.
    """

    owner: str
    name: str
    actor: WorkflowActor
    sources: tuple[SourceRegistration, ...] = ()

    def __post_init__(self) -> None:
        _owned_identity(self.owner, self.name)
        if not callable(self.actor):
            raise TypeError("actor must be callable")
        if isinstance(self.sources, (str, bytes)):
            raise TypeError("sources must be an iterable of SourceRegistration values")
        try:
            sources = tuple(self.sources)
        except TypeError as exc:
            raise TypeError(
                "sources must be an iterable of SourceRegistration values"
            ) from exc
        if not all(isinstance(source, SourceRegistration) for source in sources):
            raise TypeError(
                "sources must contain kestrel_sdk.signals.SourceRegistration values"
            )
        source_names = [source.name for source in sources]
        if len(set(source_names)) != len(source_names):
            raise ValueError("workflow source names must be unique")
        object.__setattr__(self, "sources", sources)

    @property
    def identity(self) -> tuple[str, str]:
        """Stable ``(owner, name)`` key used for deterministic teardown."""

        return (self.owner, self.name)


def _owned_identity(owner: str, name: str) -> None:
    stable_token(owner, "owner")
    stable_token(name, "name")


@runtime_checkable
class SetupStepContext(Protocol):
    """Generic context supplied by the runtime to a contributed setup step.

    The shape mirrors only the stable facilities an external setup integration
    needs. Sovereign may pass its existing context directly or an adapter; the
    feature never needs to import the concrete wizard type.
    """

    @property
    def project_dir(self) -> Path:
        """Root containing the host's configuration files."""
        ...

    @property
    def agent_data_root(self) -> Path:
        """Root containing agent data directories."""
        ...

    @property
    def flow(self) -> SetupFlow:
        """Runtime-selected setup flow classification."""
        ...

    @property
    def prompter(self) -> Any:
        """Runtime-owned prompting interface, opaque to the SDK."""
        ...

    def record(self, message: str) -> None:
        """Record a successful or informative setup change."""
        ...

    def block(self, message: str) -> None:
        """Record an actionable setup blocker."""
        ...


class SetupFlow(StrEnum):
    """Stable setup modes shared with Sovereign's existing setup runtime."""

    SETUP = "setup"
    CHECK = "check"


def normalize_setup_flow(value: SetupFlow | Enum | str) -> SetupFlow:
    """Normalize SDK, string, or legacy Sovereign ``Flow`` enum values.

    Sovereign's historic enum does not need to inherit from ``str``; its
    string-valued ``.value`` is accepted. Names are never guessed or coerced.
    """

    if isinstance(value, SetupFlow):
        return value
    candidate = value.value if isinstance(value, Enum) else value
    if not isinstance(candidate, str):
        raise TypeError("setup flow must be a SetupFlow or string-valued Enum")
    try:
        return SetupFlow(candidate)
    except ValueError as exc:
        allowed = ", ".join(flow.value for flow in SetupFlow)
        raise ValueError(f"setup flow must be one of: {allowed}") from exc


SetupStep: TypeAlias = Callable[[SetupStepContext], ContributionResult]


class SetupStepClassification(StrEnum):
    """Whether a step joins the default setup flow or is explicitly selected."""

    DEFAULT = "default"
    OPTIONAL = "optional"


@dataclass(frozen=True, slots=True)
class SetupStepRegistration:
    """A lifecycle-owned setup callable with hard ordering constraints.

    ``before`` and ``after`` are hard topological constraints, not hints. Once
    all active steps have been collected, the runtime rejects unknown names or
    cycles with :class:`ContributionContractError` and activates none of that
    transition. Among simultaneously eligible steps, ``(order, name)`` is the
    deterministic tie-break. Use :func:`order_setup_step_registrations` to
    apply this policy.
    """

    owner: str
    name: str
    step: SetupStep
    classification: SetupStepClassification = SetupStepClassification.OPTIONAL
    order: int = 1000
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _owned_identity(self.owner, self.name)
        if not callable(self.step):
            raise TypeError("step must be callable")
        classification = self.classification
        if not isinstance(classification, SetupStepClassification):
            if isinstance(classification, str):
                try:
                    classification = SetupStepClassification(classification)
                except ValueError as exc:
                    raise ValueError(
                        "classification must be 'default' or 'optional'"
                    ) from exc
            else:
                raise TypeError(
                    "classification must be a SetupStepClassification"
                )
        if isinstance(self.order, bool) or not isinstance(self.order, int):
            raise TypeError("order must be an int")
        before = unique_tuple(self.before, "before")
        after = unique_tuple(self.after, "after")
        for relative_name in (*before, *after):
            stable_token(relative_name, "ordering name")
            if relative_name == self.name:
                raise ValueError("a setup step cannot order itself")
        if set(before).intersection(after):
            raise ValueError("the same setup step cannot appear in before and after")
        object.__setattr__(self, "classification", classification)
        object.__setattr__(self, "before", before)
        object.__setattr__(self, "after", after)

    @property
    def identity(self) -> tuple[str, str]:
        """Stable ``(owner, name)`` key used for deterministic teardown."""

        return (self.owner, self.name)


_T = TypeVar("_T")


async def await_contribution_result(value: _T | Awaitable[_T]) -> _T:
    """Return a contribution result, awaiting it when necessary.

    Workflow actors and setup steps may be synchronous or asynchronous. The
    runtime must pass every returned value through this helper (or equivalent
    ``inspect.isawaitable`` logic) so coroutine results are never discarded.
    """

    if inspect.isawaitable(value):
        return await value
    return value


def order_setup_step_registrations(
    registrations: tuple[SetupStepRegistration, ...],
) -> tuple[SetupStepRegistration, ...]:
    """Apply the setup ordering contract to the complete active step set."""

    steps = _typed_tuple(
        registrations,
        SetupStepRegistration,
        "setup step registrations",
    )
    by_name: dict[str, SetupStepRegistration] = {}
    for registration in steps:
        if registration.name in by_name:
            raise ContributionContractError(
                f"duplicate setup step name: {registration.name}"
            )
        by_name[registration.name] = registration

    outgoing = {name: set() for name in by_name}
    indegree = {name: 0 for name in by_name}
    for registration in steps:
        for target in registration.before:
            _require_setup_reference(target, registration.name, by_name)
            if target not in outgoing[registration.name]:
                outgoing[registration.name].add(target)
                indegree[target] += 1
        for dependency in registration.after:
            _require_setup_reference(dependency, registration.name, by_name)
            if registration.name not in outgoing[dependency]:
                outgoing[dependency].add(registration.name)
                indegree[registration.name] += 1

    ready = [
        (registration.order, registration.name)
        for registration in steps
        if indegree[registration.name] == 0
    ]
    heapq.heapify(ready)
    ordered: list[SetupStepRegistration] = []
    while ready:
        _, name = heapq.heappop(ready)
        ordered.append(by_name[name])
        for successor in outgoing[name]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                registration = by_name[successor]
                heapq.heappush(ready, (registration.order, registration.name))
    if len(ordered) != len(steps):
        raise ContributionContractError("setup step ordering contains a cycle")
    return tuple(ordered)


def _require_setup_reference(
    reference: str,
    source: str,
    registrations: Mapping[str, SetupStepRegistration],
) -> None:
    if reference not in registrations:
        raise ContributionContractError(
            f"setup step {source!r} references unknown step {reference!r}"
        )


@dataclass(frozen=True, slots=True)
class FeatureContributionSet:
    """Validated contributions collected once for one lifecycle transition."""

    services: tuple[ServiceRegistration, ...]
    wait_providers: tuple[WaitProviderRegistration, ...]
    workflows: tuple[WorkflowRegistration, ...]
    permission_defaults: FeaturePermissionDefaults | None
    setup_steps: tuple[SetupStepRegistration, ...]


def validate_contribution_owner_uniqueness(
    contribution_owners: object,
) -> tuple[str, ...]:
    """Validate the owners for one prospective simultaneously active set.

    Row 2 must collect the exact ``contribution_owner`` from every agent and
    host feature that would be active after a lifecycle transition, call this
    helper before registering any contribution, and retain the returned tuple
    with the active lifecycle state. Duplicate owners reject the complete
    transition even when the feature classes or their contributions differ.
    """

    if isinstance(contribution_owners, (str, bytes)):
        raise ContributionContractError(
            "contribution owners must be an iterable of stable tokens"
        )
    try:
        owners = tuple(contribution_owners)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ContributionContractError(
            "contribution owners must be an iterable of stable tokens"
        ) from exc

    seen: set[str] = set()
    for owner in owners:
        try:
            stable_token(owner, "feature contribution_owner")
        except (TypeError, ValueError) as exc:
            raise ContributionContractError(str(exc)) from exc
        if owner in seen:
            raise ContributionContractError(
                f"duplicate active feature contribution_owner: {owner!r}"
            )
        seen.add(owner)
    return owners


def validate_feature_contributions(
    contribution_owner: str,
    *,
    tool_names: object,
    services: object,
    wait_providers: object,
    workflows: object,
    permission_defaults: object,
    setup_steps: object,
) -> FeatureContributionSet:
    """Validate values collected from one feature lifecycle transition.

    Row-2 calls each contribution method once per enable or host-start
    transition, passes the exact results here, and retains the returned
    canonical contribution identity, registrations, and implementation objects
    until teardown. Every registration ``owner`` must equal the contributing
    feature's exact, canonical ``feature.contribution_owner`` value. Row 2 must
    supply the feature's actual tool names so permission overrides cannot
    silently name nonexistent tools. Type, owner, and duplicate failures raise
    :class:`ContributionContractError`.
    """

    try:
        stable_token(contribution_owner, "feature contribution_owner")
    except (TypeError, ValueError) as exc:
        raise ContributionContractError(str(exc)) from exc
    try:
        actual_tool_names = frozen_tokens(tool_names, "feature tool names")
    except (TypeError, ValueError) as exc:
        raise ContributionContractError(str(exc)) from exc
    service_values = _typed_tuple(services, ServiceRegistration, "services")
    wait_values = _typed_tuple(
        wait_providers, WaitProviderRegistration, "wait providers"
    )
    workflow_values = _typed_tuple(
        workflows, WorkflowRegistration, "workflow registrations"
    )
    setup_values = _typed_tuple(
        setup_steps, SetupStepRegistration, "setup step registrations"
    )
    if permission_defaults is not None and not isinstance(
        permission_defaults, FeaturePermissionDefaults
    ):
        raise ContributionContractError(
            "permission defaults must be FeaturePermissionDefaults or None"
        )
    if permission_defaults is not None:
        unknown_overrides = set(permission_defaults.tool_overrides) - actual_tool_names
        if unknown_overrides:
            unknown = ", ".join(sorted(unknown_overrides))
            raise ContributionContractError(
                f"permission overrides reference unknown feature tools: {unknown}"
            )
    for method_name, registrations in (
        ("get_service_registrations", service_values),
        ("get_wait_provider_registrations", wait_values),
        ("get_workflow_registrations", workflow_values),
        ("get_setup_step_registrations", setup_values),
    ):
        identities: set[object] = set()
        for registration in registrations:
            if registration.owner != contribution_owner:
                raise ContributionContractError(
                    f"{method_name} declared owner {registration.owner!r}; "
                    f"expected {contribution_owner!r}"
                )
            if registration.identity in identities:
                raise ContributionContractError(
                    f"{method_name} returned duplicate identity "
                    f"{registration.identity!r}"
                )
            identities.add(registration.identity)
    source_names: set[str] = set()
    for workflow in workflow_values:
        for source in workflow.sources:
            if source.name in source_names:
                raise ContributionContractError(
                    f"duplicate workflow source name: {source.name}"
                )
            source_names.add(source.name)
    return FeatureContributionSet(
        services=service_values,
        wait_providers=wait_values,
        workflows=workflow_values,
        permission_defaults=permission_defaults,
        setup_steps=setup_values,
    )


def _typed_tuple(value: object, item_type: type[_T], field_name: str) -> tuple[_T, ...]:
    if not isinstance(value, tuple):
        raise ContributionContractError(f"{field_name} must be returned as a tuple")
    if not all(isinstance(item, item_type) for item in value):
        raise ContributionContractError(
            f"{field_name} must contain only {item_type.__name__} values"
        )
    return value


ServiceContributions: TypeAlias = tuple[ServiceRegistration, ...]
WaitProviderContributions: TypeAlias = tuple[WaitProviderRegistration, ...]
WorkflowContributions: TypeAlias = tuple[WorkflowRegistration, ...]
SetupStepContributions: TypeAlias = tuple[SetupStepRegistration, ...]


__all__ = [
    "ContributionContractError",
    "ContributionResult",
    "FeaturePermissionDefaults",
    "FeatureContributionSet",
    "PermissionLevel",
    "ServiceContributions",
    "SetupFlow",
    "SetupStep",
    "SetupStepClassification",
    "SetupStepContext",
    "SetupStepContributions",
    "SetupStepRegistration",
    "WaitProviderContributions",
    "WaitProviderRegistration",
    "WorkflowActor",
    "WorkflowContributions",
    "WorkflowRegistration",
    "await_contribution_result",
    "normalize_setup_flow",
    "order_setup_step_registrations",
    "validate_contribution_owner_uniqueness",
    "validate_feature_contributions",
]
