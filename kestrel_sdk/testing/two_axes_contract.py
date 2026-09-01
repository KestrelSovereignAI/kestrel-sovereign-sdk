"""Versioned cross-repository fixture for the Two Axes contract.

The fixture describes one deliberately divergent relationship: a peer causes
work in a spawned child, while a different agent is the child's lineage
parent.  Sovereign core and out-of-tree observability features consume this
same package resource in their contract tests.  Keeping the artifact in the
SDK avoids both copied JSON and a forbidden runtime dependency from core to a
feature package.

The loader validates the relationship invariants before returning an immutable
view.  It does not implement authorization.  Causation remains diagnostic;
each runtime must independently verify its own durable authority receipts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any

from kestrel_sdk.signals import CausationFrame


TWO_AXES_CONTRACT_SCHEMA = "kestrel.two-axes.peer-drives-spawned-child/v1"
_CONTRACT_RESOURCE = "contracts/two_axes_peer_drives_spawned_child.v1.json"
_EXPECTED_FORBIDDEN_ACTIONS = frozenset(
    {"terminate", "delegate", "cascade", "hold", "mutate"}
)
_EXPECTED_MATRIX_VALUES = {
    "storage_backends": frozenset({"sqlite", "postgresql"}),
    "load_orders": frozenset(
        {"core_then_observability", "observability_then_core"}
    ),
    "restart_states": frozenset({"before_restart", "after_cold_restart"}),
}


@dataclass(frozen=True)
class TwoAxesContractFixture:
    """Immutable peer-causation / parent-authority contract scenario."""

    schema: str
    scenario: str
    parent_did: str
    peer_did: str
    child_did: str
    task_id: str
    signed_metadata_chain: tuple[CausationFrame, ...]
    canonical_recipient_chain: tuple[CausationFrame, ...]
    causal_predecessor_did: str
    authority_holder_did: str
    missing_causation: str
    truncated_causation: str
    unsigned_lineage_authority: str
    tampered_lineage_authority: str
    valid_causation_with_invalid_lineage: str
    forbidden_authority_from_causation: frozenset[str]
    storage_backends: frozenset[str]
    load_orders: frozenset[str]
    restart_states: frozenset[str]

    def signed_metadata_wire_chain(self) -> list[dict[str, object]]:
        """Return a fresh JSON-compatible copy of the signed task chain."""

        return [_frame_to_wire(frame) for frame in self.signed_metadata_chain]

    def canonical_recipient_wire_chain(self) -> list[dict[str, object]]:
        """Return a fresh JSON-compatible copy of the post-dispatch chain."""

        return [_frame_to_wire(frame) for frame in self.canonical_recipient_chain]


def load_two_axes_contract() -> TwoAxesContractFixture:
    """Load and validate the packaged v1 Two Axes contract fixture."""

    resource = resources.files("kestrel_sdk.testing").joinpath(_CONTRACT_RESOURCE)
    with resource.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return _parse_two_axes_contract(raw)


def _parse_two_axes_contract(raw: Any) -> TwoAxesContractFixture:
    if not isinstance(raw, dict):
        raise ValueError("Two Axes contract must be a JSON object")

    schema = _required_string(raw, "$schema")
    if schema != TWO_AXES_CONTRACT_SCHEMA:
        raise ValueError(
            f"unsupported Two Axes contract schema: {schema!r}"
        )
    scenario = _required_string(raw, "scenario")
    if scenario != "peer_drives_spawned_child":
        raise ValueError(f"unsupported Two Axes scenario: {scenario!r}")

    identities = _required_mapping(raw, "identities")
    parent_did = _required_string(identities, "parent_did")
    peer_did = _required_string(identities, "peer_did")
    child_did = _required_string(identities, "child_did")
    if len({parent_did, peer_did, child_did}) != 3:
        raise ValueError("Two Axes fixture identities must be distinct")

    task = _required_mapping(raw, "a2a_task")
    task_id = _required_string(task, "id")
    signed_chain = _parse_chain(task, "signed_metadata_causation_chain")
    recipient_chain = _parse_chain(task, "canonical_recipient_chain")
    if len(signed_chain) != 1:
        raise ValueError("signed metadata must contain exactly one producer frame")
    producer = signed_chain[0]
    if (
        producer.agent_id != peer_did
        or producer.source != "a2a.task_submitted"
        or producer.signal_id != task_id
        or producer.depth != 1
    ):
        raise ValueError("signed metadata does not identify the peer producer")
    if len(recipient_chain) != 2 or recipient_chain[0] != producer:
        raise ValueError("recipient chain must preserve the signed producer frame")
    recipient = recipient_chain[1]
    if (
        recipient.agent_id != child_did
        or recipient.source != "a2a.task_submitted"
        or recipient.depth != 2
    ):
        raise ValueError("recipient chain does not append the spawned child frame")

    lineage = _required_mapping(raw, "lineage")
    if _required_string(lineage, "parent_did") != parent_did:
        raise ValueError("lineage parent must match identities.parent_did")
    if _required_string(lineage, "child_did") != child_did:
        raise ValueError("lineage child must match identities.child_did")

    expected = _required_mapping(raw, "expected")
    causal_predecessor_did = _required_string(
        expected, "causal_predecessor_did"
    )
    authority_holder_did = _required_string(expected, "authority_holder_did")
    if causal_predecessor_did != peer_did:
        raise ValueError("causal predecessor must be the peer producer")
    if authority_holder_did != parent_did:
        raise ValueError("authority holder must be the lineage parent")
    if causal_predecessor_did == authority_holder_did:
        raise ValueError("fixture must keep causation and authority divergent")

    expected_states = {
        "missing_causation": "unknown",
        "truncated_causation": "unknown",
        "unsigned_lineage_authority": "denied",
        "tampered_lineage_authority": "denied",
        "valid_causation_with_invalid_lineage": "preserved",
    }
    for key, value in expected_states.items():
        if _required_string(expected, key) != value:
            raise ValueError(f"Two Axes expectation {key!r} must be {value!r}")

    forbidden = _required_string_set(raw, "forbidden_authority_from_causation")
    if forbidden != _EXPECTED_FORBIDDEN_ACTIONS:
        raise ValueError("causation authority-denial actions are incomplete")

    matrix = _required_mapping(raw, "verification_matrix")
    matrix_values = {
        key: _required_string_set(matrix, key) for key in _EXPECTED_MATRIX_VALUES
    }
    for key, expected_values in _EXPECTED_MATRIX_VALUES.items():
        if matrix_values[key] != expected_values:
            raise ValueError(f"Two Axes verification matrix {key!r} is incomplete")

    return TwoAxesContractFixture(
        schema=schema,
        scenario=scenario,
        parent_did=parent_did,
        peer_did=peer_did,
        child_did=child_did,
        task_id=task_id,
        signed_metadata_chain=signed_chain,
        canonical_recipient_chain=recipient_chain,
        causal_predecessor_did=causal_predecessor_did,
        authority_holder_did=authority_holder_did,
        missing_causation=expected_states["missing_causation"],
        truncated_causation=expected_states["truncated_causation"],
        unsigned_lineage_authority=expected_states["unsigned_lineage_authority"],
        tampered_lineage_authority=expected_states["tampered_lineage_authority"],
        valid_causation_with_invalid_lineage=expected_states[
            "valid_causation_with_invalid_lineage"
        ],
        forbidden_authority_from_causation=forbidden,
        storage_backends=matrix_values["storage_backends"],
        load_orders=matrix_values["load_orders"],
        restart_states=matrix_values["restart_states"],
    )


def _required_mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    candidate = value.get(key)
    if not isinstance(candidate, dict):
        raise ValueError(f"Two Axes contract field {key!r} must be an object")
    return candidate


def _required_string(value: dict[str, Any], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate:
        raise ValueError(
            f"Two Axes contract field {key!r} must be a non-empty string"
        )
    return candidate


def _required_string_set(value: dict[str, Any], key: str) -> frozenset[str]:
    candidate = value.get(key)
    if (
        not isinstance(candidate, list)
        or not candidate
        or any(not isinstance(item, str) or not item for item in candidate)
        or len(set(candidate)) != len(candidate)
    ):
        raise ValueError(
            f"Two Axes contract field {key!r} must be unique non-empty strings"
        )
    return frozenset(candidate)


def _parse_chain(value: dict[str, Any], key: str) -> tuple[CausationFrame, ...]:
    raw_chain = value.get(key)
    if not isinstance(raw_chain, list) or not raw_chain:
        raise ValueError(f"Two Axes contract field {key!r} must be a non-empty list")
    frames: list[CausationFrame] = []
    for index, raw_frame in enumerate(raw_chain):
        if not isinstance(raw_frame, dict):
            raise ValueError(f"Two Axes frame {key}[{index}] must be an object")
        emitted_at_raw = _required_string(raw_frame, "emitted_at")
        try:
            emitted_at = datetime.fromisoformat(emitted_at_raw)
        except ValueError as error:
            raise ValueError(
                f"Two Axes frame {key}[{index}] has invalid emitted_at"
            ) from error
        if emitted_at.tzinfo is None:
            raise ValueError(
                f"Two Axes frame {key}[{index}] emitted_at must be timezone-aware"
            )
        depth = raw_frame.get("depth")
        if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
            raise ValueError(
                f"Two Axes frame {key}[{index}] depth must be a positive integer"
            )
        turn_id = raw_frame.get("turn_id")
        if turn_id is not None and (
            not isinstance(turn_id, str) or not turn_id
        ):
            raise ValueError(
                f"Two Axes frame {key}[{index}] turn_id must be null or non-empty"
            )
        frames.append(
            CausationFrame(
                agent_id=_required_string(raw_frame, "agent_id"),
                source=_required_string(raw_frame, "source"),
                signal_id=_required_string(raw_frame, "signal_id"),
                turn_id=turn_id,
                depth=depth,
                emitted_at=emitted_at,
            )
        )
    if [frame.depth for frame in frames] != list(range(1, len(frames) + 1)):
        raise ValueError(f"Two Axes frame depths in {key!r} must be contiguous")
    return tuple(frames)


def _frame_to_wire(frame: CausationFrame) -> dict[str, object]:
    return {
        "agent_id": frame.agent_id,
        "source": frame.source,
        "signal_id": frame.signal_id,
        "turn_id": frame.turn_id,
        "depth": frame.depth,
        "emitted_at": frame.emitted_at.isoformat(),
    }


__all__ = [
    "TWO_AXES_CONTRACT_SCHEMA",
    "TwoAxesContractFixture",
    "load_two_axes_contract",
]
