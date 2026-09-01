"""Shared Two Axes fixture contract tests (kestrel-sovereign#3200)."""

from __future__ import annotations

import copy

import pytest

from kestrel_sdk.testing import (
    TWO_AXES_CONTRACT_SCHEMA,
    TwoAxesContractFixture,
    load_two_axes_contract,
)
from kestrel_sdk.testing.two_axes_contract import _parse_two_axes_contract


def test_packaged_fixture_keeps_causation_and_authority_on_different_axes() -> None:
    fixture = load_two_axes_contract()

    assert isinstance(fixture, TwoAxesContractFixture)
    assert fixture.schema == TWO_AXES_CONTRACT_SCHEMA
    assert fixture.scenario == "peer_drives_spawned_child"
    assert fixture.causal_predecessor_did == fixture.peer_did
    assert fixture.authority_holder_did == fixture.parent_did
    assert fixture.causal_predecessor_did != fixture.authority_holder_did
    assert fixture.signed_metadata_chain[-1].agent_id == fixture.peer_did
    assert fixture.canonical_recipient_chain[-2].agent_id == fixture.peer_did
    assert fixture.canonical_recipient_chain[-1].agent_id == fixture.child_did


def test_packaged_fixture_pins_fail_closed_and_restart_matrix() -> None:
    fixture = load_two_axes_contract()

    assert fixture.missing_causation == "unknown"
    assert fixture.truncated_causation == "unknown"
    assert fixture.unsigned_lineage_authority == "denied"
    assert fixture.tampered_lineage_authority == "denied"
    assert fixture.valid_causation_with_invalid_lineage == "preserved"
    assert fixture.forbidden_authority_from_causation == {
        "terminate",
        "delegate",
        "cascade",
        "hold",
        "mutate",
    }
    assert fixture.storage_backends == {"sqlite", "postgresql"}
    assert fixture.load_orders == {
        "core_then_observability",
        "observability_then_core",
    }
    assert fixture.restart_states == {"before_restart", "after_cold_restart"}


def test_wire_chain_accessors_return_fresh_json_compatible_values() -> None:
    fixture = load_two_axes_contract()

    first = fixture.signed_metadata_wire_chain()
    first[0]["agent_id"] = "did:example:mutated"

    second = fixture.signed_metadata_wire_chain()
    assert second[0]["agent_id"] == fixture.peer_did
    assert second[0]["emitted_at"] == "2026-08-31T12:00:00+00:00"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda raw: raw["expected"].__setitem__(
                "authority_holder_did", raw["identities"]["peer_did"]
            ),
            "authority holder must be the lineage parent",
        ),
        (
            lambda raw: raw["a2a_task"][
                "signed_metadata_causation_chain"
            ][0].__setitem__("agent_id", raw["identities"]["parent_did"]),
            "signed metadata does not identify the peer producer",
        ),
        (
            lambda raw: raw["forbidden_authority_from_causation"].remove("hold"),
            "authority-denial actions are incomplete",
        ),
        (
            lambda raw: raw["verification_matrix"]["restart_states"].remove(
                "after_cold_restart"
            ),
            "verification matrix 'restart_states' is incomplete",
        ),
    ],
)
def test_parser_rejects_relationship_or_matrix_collapse(mutation, message) -> None:
    fixture = load_two_axes_contract()
    raw = {
        "$schema": fixture.schema,
        "scenario": fixture.scenario,
        "identities": {
            "parent_did": fixture.parent_did,
            "peer_did": fixture.peer_did,
            "child_did": fixture.child_did,
        },
        "a2a_task": {
            "id": fixture.task_id,
            "signed_metadata_causation_chain": fixture.signed_metadata_wire_chain(),
            "canonical_recipient_chain": fixture.canonical_recipient_wire_chain(),
        },
        "lineage": {
            "parent_did": fixture.parent_did,
            "child_did": fixture.child_did,
        },
        "expected": {
            "causal_predecessor_did": fixture.causal_predecessor_did,
            "authority_holder_did": fixture.authority_holder_did,
            "missing_causation": fixture.missing_causation,
            "truncated_causation": fixture.truncated_causation,
            "unsigned_lineage_authority": fixture.unsigned_lineage_authority,
            "tampered_lineage_authority": fixture.tampered_lineage_authority,
            "valid_causation_with_invalid_lineage": (
                fixture.valid_causation_with_invalid_lineage
            ),
        },
        "forbidden_authority_from_causation": sorted(
            fixture.forbidden_authority_from_causation
        ),
        "verification_matrix": {
            "storage_backends": sorted(fixture.storage_backends),
            "load_orders": sorted(fixture.load_orders),
            "restart_states": sorted(fixture.restart_states),
        },
    }
    raw = copy.deepcopy(raw)
    mutation(raw)

    with pytest.raises(ValueError, match=message):
        _parse_two_axes_contract(raw)
