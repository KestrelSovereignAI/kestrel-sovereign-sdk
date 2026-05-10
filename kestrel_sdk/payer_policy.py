"""
PayerPolicy — declarative model of who pays for which metered resource.

Foundation primitive shared by Kestrel Sovereign and feature packages
(Frinz, Kestrel Talon, etc.). Pure types: no IO, no vendor knowledge,
no resolver implementations. The corresponding resolver lives in the
main framework repo.

Six funding patterns are admitted (the resolver implements them):

    Standalone     — single operator runs everything from host env vars.
    Platform-pays  — host master account, child credential per agent.
    User-pays      — user master account, child credential per agent.
    Sponsor-pays   — third-party master account, child credential per agent.
    Self-pays      — agent's own wallet pays vendors directly (e.g. x402).
    None           — agent does not use this resource at all.

A `PayerPolicy` carries one `PayerSpec` per `ResourceClass`. Each spec
names a `vendor` (e.g. ``"openrouter"``, ``"lighthouse"``) and a
`PayerKind`. The `(resource_class, vendor, kind)` triple is keyed
against the `SUPPORT_MATRIX`, which is the single source of truth for
which combinations are implemented.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Iterable, Mapping, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator


# =============================================================================
# Enums
# =============================================================================


class ResourceClass(StrEnum):
    """Class of metered resource a `PayerSpec` configures."""

    LLM = "llm"
    STORAGE = "storage"
    COMPUTE = "compute"
    TOOLS = "tools"
    COMMS = "comms"


class PayerKind(StrEnum):
    """How a resource is paid for, for a given agent.

    The six funding patterns admitted by `PayerPolicy` correspond to:

    - `NONE`                     — None: agent has no access to this resource.
    - `HOST_ENV`                 — Standalone: operator's host env vars.
    - `HOST_MASTER_PROVISIONED`  — Platform-pays: operator's master account,
      child credential per agent. The master DID is the host's, implicit.
    - `USER_MASTER_PROVISIONED`  — User-pays: a named user's master
      account funds the agent. `master_did` carries the user DID.
    - `SPONSOR`                  — Sponsor-pays: a named third party's
      master account funds the agent. `master_did` carries the sponsor DID.
    - `SELF_WALLET`              — Self-pays: agent's own wallet pays
      vendors directly (e.g. x402, Lighthouse wallet-signed key).

    `USER_MASTER_PROVISIONED` and `SPONSOR` share the same on-the-wire
    mechanism (delegated master → child credential) but differ in
    *whose* consent and audit trail the funding is recorded under.
    Conflating them invites bugs in billing, accounting, and consent
    UX, so they are distinct enum values.
    """

    NONE = "none"
    HOST_ENV = "host_env"
    HOST_MASTER_PROVISIONED = "host_master_provisioned"
    USER_MASTER_PROVISIONED = "user_master_provisioned"
    SELF_WALLET = "self_wallet"
    SPONSOR = "sponsor"


class SupportStatus(StrEnum):
    """Status of a (resource_class, vendor, kind) combination."""

    READY = "ready"
    NOT_IMPLEMENTED = "not_implemented"
    OUT_OF_SCOPE = "out_of_scope"
    NOT_APPLICABLE = "not_applicable"


# =============================================================================
# Support matrix (single source of truth)
# =============================================================================
#
# Reads as: {(resource_class, vendor, kind): SupportStatus}
#
# - READY: implemented and verifiable.
# - NOT_IMPLEMENTED: enum value exists, resolver raises NotImplementedError,
#   wizard refuses to offer this combination. Phased work that is
#   explicitly deferred lands here.
# - OUT_OF_SCOPE: same as NOT_IMPLEMENTED but tracked as later work
#   beyond the current PR.
# - NOT_APPLICABLE: combination is meaningless (e.g. master-provisioning
#   for a local-disk vendor that has no master).
#
# Wizard, resolver, and verify step all read from this matrix. Tests
# assert that every READY entry has a working resolver path and every
# non-READY entry raises consistently.

SUPPORT_MATRIX: Mapping[tuple[ResourceClass, str, PayerKind], SupportStatus] = {
    # ----- LLM / openrouter -----
    (ResourceClass.LLM, "openrouter", PayerKind.HOST_ENV): SupportStatus.READY,
    (ResourceClass.LLM, "openrouter", PayerKind.HOST_MASTER_PROVISIONED): SupportStatus.READY,
    (ResourceClass.LLM, "openrouter", PayerKind.USER_MASTER_PROVISIONED): SupportStatus.READY,
    (ResourceClass.LLM, "openrouter", PayerKind.SPONSOR): SupportStatus.READY,
    (ResourceClass.LLM, "openrouter", PayerKind.SELF_WALLET): SupportStatus.NOT_IMPLEMENTED,
    (ResourceClass.LLM, "openrouter", PayerKind.NONE): SupportStatus.READY,
    # ----- LLM / local (ollama, llama.cpp, etc.) -----
    (ResourceClass.LLM, "local", PayerKind.HOST_ENV): SupportStatus.READY,
    (ResourceClass.LLM, "local", PayerKind.HOST_MASTER_PROVISIONED): SupportStatus.NOT_APPLICABLE,
    (ResourceClass.LLM, "local", PayerKind.USER_MASTER_PROVISIONED): SupportStatus.NOT_APPLICABLE,
    (ResourceClass.LLM, "local", PayerKind.SPONSOR): SupportStatus.NOT_APPLICABLE,
    (ResourceClass.LLM, "local", PayerKind.SELF_WALLET): SupportStatus.NOT_APPLICABLE,
    (ResourceClass.LLM, "local", PayerKind.NONE): SupportStatus.READY,
    # ----- Storage / lighthouse -----
    # Lighthouse SELF_WALLET uses the agent's own secp256k1 key: sign
    # Lighthouse's auth message, create an API key, and store the resulting
    # credential in ServiceKeyStorage. Delegated-master Lighthouse needs a
    # separate payer-wallet custody/consent path before it can be READY.
    (ResourceClass.STORAGE, "lighthouse", PayerKind.HOST_ENV): SupportStatus.READY,
    (ResourceClass.STORAGE, "lighthouse", PayerKind.HOST_MASTER_PROVISIONED): SupportStatus.NOT_IMPLEMENTED,
    (ResourceClass.STORAGE, "lighthouse", PayerKind.USER_MASTER_PROVISIONED): SupportStatus.NOT_IMPLEMENTED,
    (ResourceClass.STORAGE, "lighthouse", PayerKind.SPONSOR): SupportStatus.NOT_IMPLEMENTED,
    (ResourceClass.STORAGE, "lighthouse", PayerKind.SELF_WALLET): SupportStatus.READY,
    (ResourceClass.STORAGE, "lighthouse", PayerKind.NONE): SupportStatus.READY,
    # ----- Storage / local-disk -----
    (ResourceClass.STORAGE, "local-disk", PayerKind.HOST_ENV): SupportStatus.READY,
    (ResourceClass.STORAGE, "local-disk", PayerKind.HOST_MASTER_PROVISIONED): SupportStatus.NOT_APPLICABLE,
    (ResourceClass.STORAGE, "local-disk", PayerKind.USER_MASTER_PROVISIONED): SupportStatus.NOT_APPLICABLE,
    (ResourceClass.STORAGE, "local-disk", PayerKind.SPONSOR): SupportStatus.NOT_APPLICABLE,
    (ResourceClass.STORAGE, "local-disk", PayerKind.SELF_WALLET): SupportStatus.NOT_APPLICABLE,
    (ResourceClass.STORAGE, "local-disk", PayerKind.NONE): SupportStatus.READY,
    # ----- Compute / generic -----
    # Compute (Vast.ai, RunPod, Hyperbolic, ...) is host_env-only in this PR.
    (ResourceClass.COMPUTE, "*", PayerKind.HOST_ENV): SupportStatus.READY,
    (ResourceClass.COMPUTE, "*", PayerKind.HOST_MASTER_PROVISIONED): SupportStatus.OUT_OF_SCOPE,
    (ResourceClass.COMPUTE, "*", PayerKind.USER_MASTER_PROVISIONED): SupportStatus.OUT_OF_SCOPE,
    (ResourceClass.COMPUTE, "*", PayerKind.SPONSOR): SupportStatus.OUT_OF_SCOPE,
    (ResourceClass.COMPUTE, "*", PayerKind.SELF_WALLET): SupportStatus.OUT_OF_SCOPE,
    (ResourceClass.COMPUTE, "*", PayerKind.NONE): SupportStatus.READY,
    # ----- Tools / generic (Tavily, Exa, ElevenLabs, ...) -----
    (ResourceClass.TOOLS, "*", PayerKind.HOST_ENV): SupportStatus.READY,
    (ResourceClass.TOOLS, "*", PayerKind.HOST_MASTER_PROVISIONED): SupportStatus.OUT_OF_SCOPE,
    (ResourceClass.TOOLS, "*", PayerKind.USER_MASTER_PROVISIONED): SupportStatus.OUT_OF_SCOPE,
    (ResourceClass.TOOLS, "*", PayerKind.SPONSOR): SupportStatus.OUT_OF_SCOPE,
    (ResourceClass.TOOLS, "*", PayerKind.SELF_WALLET): SupportStatus.OUT_OF_SCOPE,
    (ResourceClass.TOOLS, "*", PayerKind.NONE): SupportStatus.READY,
    # ----- Comms / generic (Twilio, Resend, ...) -----
    (ResourceClass.COMMS, "*", PayerKind.HOST_ENV): SupportStatus.READY,
    (ResourceClass.COMMS, "*", PayerKind.HOST_MASTER_PROVISIONED): SupportStatus.OUT_OF_SCOPE,
    (ResourceClass.COMMS, "*", PayerKind.USER_MASTER_PROVISIONED): SupportStatus.OUT_OF_SCOPE,
    (ResourceClass.COMMS, "*", PayerKind.SPONSOR): SupportStatus.OUT_OF_SCOPE,
    (ResourceClass.COMMS, "*", PayerKind.SELF_WALLET): SupportStatus.OUT_OF_SCOPE,
    (ResourceClass.COMMS, "*", PayerKind.NONE): SupportStatus.READY,
}

# Vendor wildcards — `(resource, "*", kind)` matches any vendor in that class
# whose entry is not explicitly listed. Concrete vendor entries (e.g.
# `(LLM, "openrouter", HOST_ENV)`) take precedence over wildcards.
_VENDOR_WILDCARD = "*"


def status_for(
    resource_class: ResourceClass,
    vendor: str,
    kind: PayerKind,
) -> SupportStatus:
    """Resolve the support status for a `(resource_class, vendor, kind)` triple.

    Concrete vendor entries take precedence over wildcards. Unknown
    triples (no concrete and no wildcard entry) return
    `SupportStatus.NOT_IMPLEMENTED`.
    """
    concrete = SUPPORT_MATRIX.get((resource_class, vendor, kind))
    if concrete is not None:
        return concrete
    wildcard = SUPPORT_MATRIX.get((resource_class, _VENDOR_WILDCARD, kind))
    if wildcard is not None:
        return wildcard
    return SupportStatus.NOT_IMPLEMENTED


def is_offerable(
    resource_class: ResourceClass,
    vendor: str,
    kind: PayerKind,
) -> bool:
    """True when this combination should be offered by the setup wizard."""
    return status_for(resource_class, vendor, kind) is SupportStatus.READY


def supported_kinds_for(
    resource_class: ResourceClass,
    vendor: str,
) -> tuple[PayerKind, ...]:
    """All kinds that the wizard may offer for this `(resource, vendor)`."""
    return tuple(k for k in PayerKind if is_offerable(resource_class, vendor, k))


# =============================================================================
# Resolver protocols
# =============================================================================


@runtime_checkable
class KeyResolverProtocol(Protocol):
    """The contract `ResolvedResource.key_resolver` exposes.

    Implemented in the main repo by `KeyResolutionService`; SDK
    consumers only see this protocol so they can stay framework-light.
    """

    async def resolve_key(
        self,
        provider: str,
        require: bool = True,
    ) -> Optional[str]:
        ...


class ResolvedResource(BaseModel):
    """Result of `PayerResolver.resolve_for(agent_did, resource_class)`.

    `enabled=False` means the agent's policy explicitly disables this
    resource class (`PayerKind.NONE`) — the agent-init layer must NOT
    construct providers for this slot. Returning a sentinel
    `KeyResolutionService` is insufficient because providers may have
    constructor-time env-var fallbacks; only the agent-init layer can
    reliably honor `NONE`.

    `enabled=True` carries a `key_resolver` that providers consult to
    obtain a credential. The resolver may have side-effected before
    returning (e.g., minted a child key, signed a wallet message,
    persisted into `ServiceKeyStorage`).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    enabled: bool
    key_resolver: Optional[Any] = None  # KeyResolverProtocol at runtime
    # `Any` because pydantic 2's `Protocol` validation of arbitrary
    # objects is limited; runtime callers `isinstance(x, KeyResolverProtocol)`
    # for type-narrowing if needed.

    @classmethod
    def disabled(cls) -> "ResolvedResource":
        return cls(enabled=False, key_resolver=None)


@runtime_checkable
class PayerResolver(Protocol):
    """Resolves a `PayerPolicy` slot to credentials for an agent.

    Called once per agent at agent-init time (not at every provider
    call). Side effects (provisioning a child credential, signing a
    wallet message, storing in encrypted key storage) happen here so
    providers stay simple downstream.
    """

    async def resolve_for(
        self,
        agent_did: str,
        resource_class: ResourceClass,
    ) -> ResolvedResource:
        ...


# =============================================================================
# Policy schema
# =============================================================================


class PayerSpec(BaseModel):
    """How a single resource class is paid for by a single agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vendor: str = Field(
        ...,
        min_length=1,
        description=(
            "Vendor or backend name. e.g. 'openrouter', 'lighthouse', "
            "'local', 'local-disk'. Free-form string so feature packages "
            "can introduce new vendors without an SDK change."
        ),
    )
    kind: PayerKind = Field(
        ...,
        description="How the agent's use of this vendor is paid for.",
    )
    master_did: Optional[str] = Field(
        default=None,
        description=(
            "DID of the principal whose master credentials fund this "
            "agent's use of the vendor. REQUIRED when `kind` is "
            "`USER_MASTER_PROVISIONED` (carries the user DID) or "
            "`SPONSOR` (carries the sponsor DID). Must NOT be set for "
            "any other kind (the host's master is implicit for "
            "`HOST_MASTER_PROVISIONED`; `HOST_ENV`, `SELF_WALLET`, and "
            "`NONE` have no master concept)."
        ),
    )
    monthly_cap_usd: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description=(
            "Advisory monthly cap in USD. The resolver may pass this "
            "through to vendor-side enforcement (e.g. OpenRouter's "
            "`limit_usd`) but is not obligated to enforce it locally."
        ),
    )

    @model_validator(mode="after")
    def _check_master_did(self) -> "PayerSpec":
        kinds_requiring_master = {
            PayerKind.USER_MASTER_PROVISIONED,
            PayerKind.SPONSOR,
        }
        if self.kind in kinds_requiring_master and not self.master_did:
            raise ValueError(
                f"PayerSpec(kind={self.kind.value}) requires `master_did` "
                f"to identify the principal funding the agent."
            )
        if self.kind not in kinds_requiring_master and self.master_did is not None:
            raise ValueError(
                f"PayerSpec(kind={self.kind.value}) must NOT set `master_did`; "
                f"that field is only meaningful for "
                f"`user_master_provisioned` and `sponsor` kinds."
            )
        return self

    def validate_against_matrix(
        self,
        resource_class: ResourceClass,
    ) -> None:
        """Raise `UnsupportedCombinationError` if the spec's
        `(resource, vendor, kind)` is not `READY`.

        Used by the wizard before persisting a policy and by
        `PayerPolicy.model_validator` after construction.
        """
        status = status_for(resource_class, self.vendor, self.kind)
        if status is not SupportStatus.READY:
            raise UnsupportedCombinationError(
                resource_class=resource_class,
                vendor=self.vendor,
                kind=self.kind,
                status=status,
            )

    def matrix_status(
        self,
        resource_class: ResourceClass,
    ) -> SupportStatus:
        return status_for(resource_class, self.vendor, self.kind)


class PayerPolicy(BaseModel):
    """A complete payer policy for one agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    llm: PayerSpec
    storage: PayerSpec
    compute: PayerSpec
    tools: PayerSpec
    comms: PayerSpec

    # ---- defaults ----

    @classmethod
    def host_env_default(cls) -> "PayerPolicy":
        """Today's behavior expressed as a policy.

        Every resource class is `host_env` for its de-facto vendor.
        Operators who never run the wizard get this — no agent's
        capabilities change relative to current main.
        """
        return cls(
            llm=PayerSpec(vendor="openrouter", kind=PayerKind.HOST_ENV),
            storage=PayerSpec(vendor="lighthouse", kind=PayerKind.HOST_ENV),
            compute=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
            tools=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
            comms=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
        )

    # ---- TOML round-trip ----
    #
    # The wizard writes/reads kestrel.toml under `[payments]`. The TOML
    # shape is the same as `model_dump(mode="json")` produces — strings
    # for enums, decimals serialized as strings to preserve precision.

    def to_toml_section(self) -> dict[str, Any]:
        """Serialize to a dict suitable for the kestrel.toml `[payments]` table."""
        return self.model_dump(mode="json", exclude_none=True)

    @classmethod
    def from_toml_section(cls, data: Mapping[str, Any]) -> "PayerPolicy":
        """Parse from a kestrel.toml `[payments]` table.

        Strict: rejects unknown keys. Use `from_toml_section_lenient`
        if forward-compat with newer schema versions is required.
        """
        return cls.model_validate(data)

    # ---- matrix consistency ----

    def validate_against_matrix(self) -> None:
        """Raise on any spec whose `(resource, vendor, kind)` is not READY."""
        for resource_class, spec in self._iter_specs():
            spec.validate_against_matrix(resource_class)

    def _iter_specs(self) -> Iterable[tuple[ResourceClass, PayerSpec]]:
        yield (ResourceClass.LLM, self.llm)
        yield (ResourceClass.STORAGE, self.storage)
        yield (ResourceClass.COMPUTE, self.compute)
        yield (ResourceClass.TOOLS, self.tools)
        yield (ResourceClass.COMMS, self.comms)


# =============================================================================
# Errors
# =============================================================================


class PayerPolicyError(Exception):
    """Base class for PayerPolicy-related errors."""


class UnsupportedCombinationError(PayerPolicyError):
    """Raised when a `(resource_class, vendor, kind)` is not supported.

    Carries enough information for the wizard to render a remediation
    hint ("Lighthouse SELF_WALLET is supported; Lighthouse XYZ is not")
    and for the resolver to raise a uniform error before attempting
    a side-effect.
    """

    def __init__(
        self,
        resource_class: ResourceClass,
        vendor: str,
        kind: PayerKind,
        status: SupportStatus,
    ) -> None:
        self.resource_class = resource_class
        self.vendor = vendor
        self.kind = kind
        self.status = status
        super().__init__(
            f"PayerPolicy: ({resource_class.value}, {vendor!r}, {kind.value}) "
            f"is {status.value}; not offerable"
        )


__all__ = [
    "ResourceClass",
    "PayerKind",
    "SupportStatus",
    "SUPPORT_MATRIX",
    "status_for",
    "is_offerable",
    "supported_kinds_for",
    "KeyResolverProtocol",
    "ResolvedResource",
    "PayerResolver",
    "PayerSpec",
    "PayerPolicy",
    "PayerPolicyError",
    "UnsupportedCombinationError",
]
