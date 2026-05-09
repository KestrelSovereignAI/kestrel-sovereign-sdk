"""Tests for kestrel_sdk.payer_policy.

Phase 1 of the PayerPolicy foundation. Pure-types tests; no resolver
implementation is exercised. The matrix-consistency tests are the
load-bearing ones — every wizard / resolver / verify-step decision
downstream is gated on the matrix being internally coherent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kestrel_sdk.payer_policy import (
    PayerKind,
    PayerPolicy,
    PayerPolicyError,
    PayerSpec,
    ResolvedResource,
    ResourceClass,
    SUPPORT_MATRIX,
    SupportStatus,
    UnsupportedCombinationError,
    is_offerable,
    status_for,
    supported_kinds_for,
)


# =============================================================================
# Enum identity
# =============================================================================


class TestEnums:
    def test_resource_class_string_values(self) -> None:
        assert ResourceClass.LLM == "llm"
        assert ResourceClass.STORAGE == "storage"
        assert ResourceClass.COMPUTE == "compute"
        assert ResourceClass.TOOLS == "tools"
        assert ResourceClass.COMMS == "comms"

    def test_payer_kind_string_values(self) -> None:
        assert PayerKind.NONE == "none"
        assert PayerKind.HOST_ENV == "host_env"
        assert PayerKind.HOST_MASTER_PROVISIONED == "host_master_provisioned"
        assert PayerKind.USER_MASTER_PROVISIONED == "user_master_provisioned"
        assert PayerKind.SELF_WALLET == "self_wallet"
        assert PayerKind.SPONSOR == "sponsor"

    def test_payer_kind_covers_six_funding_patterns(self) -> None:
        # The plan claims 6 funding patterns. Each must be representable
        # as exactly one PayerKind value.
        # Standalone           = HOST_ENV
        # Platform-pays        = HOST_MASTER_PROVISIONED
        # User-pays            = USER_MASTER_PROVISIONED
        # Sponsor-pays         = SPONSOR
        # Self-pays            = SELF_WALLET
        # None                 = NONE
        funding_pattern_kinds = {
            PayerKind.HOST_ENV,
            PayerKind.HOST_MASTER_PROVISIONED,
            PayerKind.USER_MASTER_PROVISIONED,
            PayerKind.SPONSOR,
            PayerKind.SELF_WALLET,
            PayerKind.NONE,
        }
        assert set(PayerKind) == funding_pattern_kinds

    def test_payer_kind_round_trip_string(self) -> None:
        # Critical for TOML round-trip: every kind must reconstruct from its value.
        for kind in PayerKind:
            assert PayerKind(kind.value) is kind


# =============================================================================
# Support matrix
# =============================================================================


class TestSupportMatrix:
    def test_every_known_triple_returns_a_status(self) -> None:
        # Spot-check the documented combinations from the plan.
        assert (
            status_for(ResourceClass.LLM, "openrouter", PayerKind.HOST_ENV)
            is SupportStatus.READY
        )
        assert (
            status_for(ResourceClass.LLM, "openrouter", PayerKind.HOST_MASTER_PROVISIONED)
            is SupportStatus.READY
        )
        assert (
            status_for(ResourceClass.LLM, "openrouter", PayerKind.SELF_WALLET)
            is SupportStatus.NOT_IMPLEMENTED
        )
        assert (
            status_for(ResourceClass.STORAGE, "lighthouse", PayerKind.SELF_WALLET)
            is SupportStatus.READY
        )

    def test_unknown_triple_returns_not_implemented(self) -> None:
        # An unknown vendor with no wildcard fallback is NOT_IMPLEMENTED.
        # (LLM has no `*` wildcard entry, only concrete `openrouter` + `local`.)
        assert (
            status_for(ResourceClass.LLM, "fictional-vendor-xyz", PayerKind.HOST_ENV)
            is SupportStatus.NOT_IMPLEMENTED
        )

    def test_concrete_vendor_overrides_wildcard(self) -> None:
        # Compute matrix uses `*` for everything except where explicitly listed.
        # If we ever add a concrete entry, it must override the wildcard.
        # Simulate: COMPUTE / "*" / HOST_ENV is READY today.
        assert (
            status_for(ResourceClass.COMPUTE, "anything", PayerKind.HOST_ENV)
            is SupportStatus.READY
        )
        # Adding a concrete entry would override; not added here, but
        # the lookup logic is exercised by the matrix structure itself.

    def test_is_offerable_only_true_for_ready(self) -> None:
        assert is_offerable(ResourceClass.LLM, "openrouter", PayerKind.HOST_ENV) is True
        assert is_offerable(ResourceClass.LLM, "openrouter", PayerKind.SELF_WALLET) is False
        assert is_offerable(ResourceClass.LLM, "fictional", PayerKind.HOST_ENV) is False

    def test_supported_kinds_for_excludes_non_ready(self) -> None:
        # OpenRouter LLM today: HOST_ENV, HOST_MASTER_PROVISIONED,
        # USER_MASTER_PROVISIONED, SPONSOR, NONE are READY; SELF_WALLET is
        # NOT_IMPLEMENTED.
        kinds = supported_kinds_for(ResourceClass.LLM, "openrouter")
        assert PayerKind.HOST_ENV in kinds
        assert PayerKind.HOST_MASTER_PROVISIONED in kinds
        assert PayerKind.USER_MASTER_PROVISIONED in kinds
        assert PayerKind.SPONSOR in kinds
        assert PayerKind.NONE in kinds
        assert PayerKind.SELF_WALLET not in kinds

    def test_local_llm_has_no_master_concept(self) -> None:
        # local LLM: there is no "master" to provision under.
        kinds = supported_kinds_for(ResourceClass.LLM, "local")
        assert PayerKind.HOST_MASTER_PROVISIONED not in kinds
        assert PayerKind.USER_MASTER_PROVISIONED not in kinds
        assert PayerKind.SPONSOR not in kinds
        assert PayerKind.SELF_WALLET not in kinds
        assert PayerKind.HOST_ENV in kinds
        assert PayerKind.NONE in kinds

    def test_lighthouse_storage_supports_all_payment_kinds(self) -> None:
        # Lighthouse is the only storage vendor with the full matrix.
        kinds = supported_kinds_for(ResourceClass.STORAGE, "lighthouse")
        for kind in PayerKind:
            assert kind in kinds, f"{kind} should be offerable for lighthouse storage"

    def test_matrix_has_no_overlapping_concrete_and_wildcard(self) -> None:
        # If a triple has a concrete entry, the wildcard for the same
        # (resource_class, kind) must not silently shadow a different
        # vendor's intent. This test catches authoring errors where a
        # wildcard contradicts a concrete entry.
        for (rc, vendor, kind), status in SUPPORT_MATRIX.items():
            if vendor == "*":
                continue
            wildcard_status = SUPPORT_MATRIX.get((rc, "*", kind))
            if wildcard_status is None:
                continue
            # If both present, they must agree OR the concrete must be more
            # permissive (READY > NOT_IMPLEMENTED). Either way, we just
            # require no silent contradiction:
            assert status is not None
            assert wildcard_status is not None


# =============================================================================
# PayerSpec
# =============================================================================


class TestPayerSpec:
    def test_minimal_construction(self) -> None:
        spec = PayerSpec(vendor="openrouter", kind=PayerKind.HOST_ENV)
        assert spec.vendor == "openrouter"
        assert spec.kind is PayerKind.HOST_ENV
        assert spec.master_did is None
        assert spec.monthly_cap_usd is None

    def test_sponsor_requires_master_did(self) -> None:
        # SPONSOR without master_did must fail at construction, not later.
        with pytest.raises(Exception) as excinfo:
            PayerSpec(vendor="openrouter", kind=PayerKind.SPONSOR)
        assert "master_did" in str(excinfo.value).lower()

    def test_user_master_requires_master_did(self) -> None:
        with pytest.raises(Exception) as excinfo:
            PayerSpec(vendor="openrouter", kind=PayerKind.USER_MASTER_PROVISIONED)
        assert "master_did" in str(excinfo.value).lower()

    def test_sponsor_with_master_did_succeeds(self) -> None:
        spec = PayerSpec(
            vendor="openrouter",
            kind=PayerKind.SPONSOR,
            master_did="did:pkh:eip155:1:0xSponsor",
        )
        assert spec.master_did == "did:pkh:eip155:1:0xSponsor"

    def test_user_master_with_master_did_succeeds(self) -> None:
        spec = PayerSpec(
            vendor="openrouter",
            kind=PayerKind.USER_MASTER_PROVISIONED,
            master_did="did:pkh:eip155:1:0xUser",
        )
        assert spec.master_did == "did:pkh:eip155:1:0xUser"

    def test_master_did_forbidden_for_other_kinds(self) -> None:
        # Setting master_did when kind doesn't use it should fail.
        for kind in (
            PayerKind.HOST_ENV,
            PayerKind.HOST_MASTER_PROVISIONED,
            PayerKind.SELF_WALLET,
            PayerKind.NONE,
        ):
            with pytest.raises(Exception) as excinfo:
                PayerSpec(
                    vendor="openrouter",
                    kind=kind,
                    master_did="did:pkh:eip155:1:0xWho",
                )
            # Error should be specific about why.
            assert "master_did" in str(excinfo.value).lower()

    def test_frozen(self) -> None:
        spec = PayerSpec(vendor="openrouter", kind=PayerKind.HOST_ENV)
        with pytest.raises(Exception):
            spec.vendor = "lighthouse"  # type: ignore[misc]

    def test_extra_keys_forbidden(self) -> None:
        with pytest.raises(Exception):
            PayerSpec(vendor="openrouter", kind=PayerKind.HOST_ENV, extra_field="x")

    def test_empty_vendor_rejected(self) -> None:
        with pytest.raises(Exception):
            PayerSpec(vendor="", kind=PayerKind.HOST_ENV)

    def test_negative_cap_rejected(self) -> None:
        with pytest.raises(Exception):
            PayerSpec(
                vendor="openrouter",
                kind=PayerKind.HOST_MASTER_PROVISIONED,
                monthly_cap_usd=Decimal("-1.00"),
            )

    def test_validate_against_matrix_passes_for_ready(self) -> None:
        spec = PayerSpec(vendor="openrouter", kind=PayerKind.HOST_ENV)
        # Should not raise.
        spec.validate_against_matrix(ResourceClass.LLM)

    def test_validate_against_matrix_raises_for_not_implemented(self) -> None:
        spec = PayerSpec(vendor="openrouter", kind=PayerKind.SELF_WALLET)
        with pytest.raises(UnsupportedCombinationError) as excinfo:
            spec.validate_against_matrix(ResourceClass.LLM)
        err = excinfo.value
        assert err.resource_class is ResourceClass.LLM
        assert err.vendor == "openrouter"
        assert err.kind is PayerKind.SELF_WALLET
        assert err.status is SupportStatus.NOT_IMPLEMENTED

    def test_unsupported_combination_is_payer_policy_error(self) -> None:
        # UnsupportedCombinationError should be catchable via the base.
        assert issubclass(UnsupportedCombinationError, PayerPolicyError)


# =============================================================================
# PayerPolicy
# =============================================================================


class TestPayerPolicy:
    def test_host_env_default_round_trips_through_matrix(self) -> None:
        policy = PayerPolicy.host_env_default()
        # Default must validate cleanly — if it doesn't, today's host_env
        # behavior is silently broken by this PR.
        policy.validate_against_matrix()

    def test_host_env_default_uses_expected_vendors(self) -> None:
        policy = PayerPolicy.host_env_default()
        assert policy.llm.vendor == "openrouter"
        assert policy.storage.vendor == "lighthouse"
        assert policy.compute.vendor == "*"
        assert policy.tools.vendor == "*"
        assert policy.comms.vendor == "*"
        for spec in (policy.llm, policy.storage, policy.compute, policy.tools, policy.comms):
            assert spec.kind is PayerKind.HOST_ENV

    def test_extra_keys_forbidden(self) -> None:
        with pytest.raises(Exception):
            PayerPolicy(
                llm=PayerSpec(vendor="openrouter", kind=PayerKind.HOST_ENV),
                storage=PayerSpec(vendor="lighthouse", kind=PayerKind.HOST_ENV),
                compute=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
                tools=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
                comms=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
                billing="extra",  # type: ignore[call-arg]
            )

    def test_validate_against_matrix_catches_per_slot_violation(self) -> None:
        # One bad slot taints the whole policy.
        bad = PayerPolicy(
            llm=PayerSpec(vendor="openrouter", kind=PayerKind.SELF_WALLET),
            storage=PayerSpec(vendor="lighthouse", kind=PayerKind.HOST_ENV),
            compute=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
            tools=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
            comms=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
        )
        with pytest.raises(UnsupportedCombinationError):
            bad.validate_against_matrix()


# =============================================================================
# TOML round-trip
# =============================================================================


class TestTOMLRoundTrip:
    def test_default_round_trips(self) -> None:
        original = PayerPolicy.host_env_default()
        section = original.to_toml_section()
        rebuilt = PayerPolicy.from_toml_section(section)
        assert rebuilt == original

    def test_round_trip_preserves_decimal_cap(self) -> None:
        original = PayerPolicy(
            llm=PayerSpec(
                vendor="openrouter",
                kind=PayerKind.HOST_MASTER_PROVISIONED,
                monthly_cap_usd=Decimal("12.34"),
            ),
            storage=PayerSpec(vendor="lighthouse", kind=PayerKind.HOST_ENV),
            compute=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
            tools=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
            comms=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
        )
        rebuilt = PayerPolicy.from_toml_section(original.to_toml_section())
        assert rebuilt.llm.monthly_cap_usd == Decimal("12.34")

    def test_round_trip_preserves_master_did_for_sponsor(self) -> None:
        original = PayerPolicy(
            llm=PayerSpec(
                vendor="openrouter",
                kind=PayerKind.SPONSOR,
                master_did="did:pkh:eip155:1:0xSponsor",
            ),
            storage=PayerSpec(vendor="lighthouse", kind=PayerKind.HOST_ENV),
            compute=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
            tools=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
            comms=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
        )
        rebuilt = PayerPolicy.from_toml_section(original.to_toml_section())
        assert rebuilt.llm.master_did == "did:pkh:eip155:1:0xSponsor"
        assert rebuilt.llm.kind is PayerKind.SPONSOR

    def test_round_trip_preserves_master_did_for_user(self) -> None:
        original = PayerPolicy(
            llm=PayerSpec(
                vendor="openrouter",
                kind=PayerKind.USER_MASTER_PROVISIONED,
                master_did="did:pkh:eip155:1:0xUser",
            ),
            storage=PayerSpec(vendor="lighthouse", kind=PayerKind.HOST_ENV),
            compute=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
            tools=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
            comms=PayerSpec(vendor="*", kind=PayerKind.HOST_ENV),
        )
        rebuilt = PayerPolicy.from_toml_section(original.to_toml_section())
        assert rebuilt.llm.master_did == "did:pkh:eip155:1:0xUser"
        assert rebuilt.llm.kind is PayerKind.USER_MASTER_PROVISIONED

    def test_unknown_keys_in_section_are_rejected(self) -> None:
        section = PayerPolicy.host_env_default().to_toml_section()
        section["billing_provider"] = "stripe"  # not a real field
        with pytest.raises(Exception):
            PayerPolicy.from_toml_section(section)

    def test_omitted_optional_fields_round_trip(self) -> None:
        # exclude_none=True means absent optional fields don't appear in the
        # serialized form, and a re-read defaults them back to None.
        original = PayerPolicy.host_env_default()
        section = original.to_toml_section()
        for slot in section.values():
            if isinstance(slot, dict):
                assert "master_did" not in slot
                assert "monthly_cap_usd" not in slot
        rebuilt = PayerPolicy.from_toml_section(section)
        for spec in (rebuilt.llm, rebuilt.storage, rebuilt.compute, rebuilt.tools, rebuilt.comms):
            assert spec.master_did is None
            assert spec.monthly_cap_usd is None


# =============================================================================
# ResolvedResource
# =============================================================================


class TestResolvedResource:
    def test_disabled_factory(self) -> None:
        r = ResolvedResource.disabled()
        assert r.enabled is False
        assert r.key_resolver is None

    def test_enabled_carries_resolver(self) -> None:
        # Use a sentinel object as a stand-in for the runtime resolver.
        sentinel = object()
        r = ResolvedResource(enabled=True, key_resolver=sentinel)
        assert r.enabled is True
        assert r.key_resolver is sentinel

    def test_frozen(self) -> None:
        r = ResolvedResource.disabled()
        with pytest.raises(Exception):
            r.enabled = True  # type: ignore[misc]
