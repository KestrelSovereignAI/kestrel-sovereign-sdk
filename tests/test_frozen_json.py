"""The shared bounded JSON freeze used by public SDK contracts."""

from __future__ import annotations

import json

import pytest

from kestrel_sdk._frozen_json import (
    JSONLimitError,
    JSONLimits,
    JSONTypeError,
    canonical_json_bytes,
    freeze_json,
    hashable_json,
    thaw_json,
)


def test_freeze_copies_deeply_and_is_read_only() -> None:
    source = {"a": {"b": [1, 2]}, "c": "x"}
    frozen = freeze_json(source, path="value").value
    source["a"]["b"].append(3)  # type: ignore[index]

    assert frozen["a"]["b"] == (1, 2)  # type: ignore[index]
    with pytest.raises(TypeError):
        frozen["new"] = 1  # type: ignore[index]
    assert thaw_json(frozen) == {"a": {"b": [1, 2]}, "c": "x"}


def test_self_reference_is_a_cycle_error_not_recursion() -> None:
    looped: dict[str, object] = {}
    looped["self"] = looped
    with pytest.raises(JSONLimitError) as excinfo:
        freeze_json(looped, path="state")
    assert excinfo.value.rule == "cycle"

    items: list[object] = []
    items.append(items)
    with pytest.raises(JSONLimitError, match="cycle"):
        freeze_json(items, path="state")


def test_shared_but_acyclic_containers_are_not_cycles() -> None:
    shared = {"k": 1}
    frozen = freeze_json({"a": shared, "b": shared}, path="value").value
    assert thaw_json(frozen) == {"a": {"k": 1}, "b": {"k": 1}}


def test_non_string_keys_are_rejected_by_default() -> None:
    # json.dumps would silently coerce 1 -> "1" and collide with "1".
    with pytest.raises(JSONTypeError) as excinfo:
        freeze_json({1: "a", "1": "b"}, path="state")
    assert excinfo.value.rule == "key"


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(number: float) -> None:
    with pytest.raises(JSONLimitError, match="non-finite") as excinfo:
        freeze_json({"n": number}, path="state")
    assert excinfo.value.rule == "number"


def test_unsupported_types_are_rejected() -> None:
    with pytest.raises(JSONTypeError, match="JSON-like") as excinfo:
        freeze_json({"when": object()}, path="state")
    assert excinfo.value.rule == "type"


def test_depth_nodes_and_keys_budgets() -> None:
    nested: object = "leaf"
    for _ in range(4):
        nested = {"n": nested}
    with pytest.raises(JSONLimitError, match="levels"):
        freeze_json(nested, path="v", limits=JSONLimits(max_depth=3))
    with pytest.raises(JSONLimitError, match="nodes"):
        freeze_json([None] * 5, path="v", limits=JSONLimits(max_nodes=5))
    with pytest.raises(JSONLimitError, match="keys"):
        freeze_json({"a": 1, "b": 2}, path="v", limits=JSONLimits(max_keys=1))


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        {"a": 1, "b": [True, None, 2.5, "ünïcødé ✓"], "c": {"d": ""}},
        ["x", {"y": [1, [2, [3]]]}],
        "plain",
        0,
        None,
    ],
)
def test_measured_size_equals_canonical_encoding(value: object) -> None:
    frozen = freeze_json(
        value, path="v", limits=JSONLimits(max_encoded_bytes=1 << 20)
    )
    expected = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    assert frozen.encoded_bytes == len(expected)
    assert canonical_json_bytes(frozen.value) == expected


def test_size_is_not_measured_without_a_budget() -> None:
    assert freeze_json({"a": 1}, path="v").encoded_bytes is None


def test_byte_budget_stops_the_walk_early() -> None:
    seen: list[str] = []

    def record(value: str, path: str) -> str:
        seen.append(value)
        return value

    value = ["x" * 10 for _ in range(1000)]
    with pytest.raises(JSONLimitError) as excinfo:
        freeze_json(
            value,
            path="state",
            limits=JSONLimits(max_encoded_bytes=100),
            validate_string=record,
        )
    assert excinfo.value.rule == "bytes"
    # Each item costs 12 bytes plus a comma, so the walk stops within the
    # first handful of strings instead of visiting all 1000.
    assert len(seen) < 10


def test_domain_validators_can_rewrite_and_reject() -> None:
    def upper_key(key: object, path: str) -> str:
        if not isinstance(key, str):
            raise TypeError("keys must be strings")
        if key == "forbidden":
            raise ValueError(f"{path} key not allowed")
        return key.upper()

    frozen = freeze_json({"a": 1}, path="v", validate_key=upper_key).value
    assert dict(frozen) == {"A": 1}  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not allowed"):
        freeze_json({"forbidden": 1}, path="v", validate_key=upper_key)


def test_hashable_json_is_equality_consistent() -> None:
    left = freeze_json({"a": [1, {"b": 2}], "c": 3}, path="v").value
    right = freeze_json({"c": 3, "a": [1, {"b": 2}]}, path="v").value
    assert hashable_json(left) == hashable_json(right)
    assert hash(hashable_json(left)) == hash(hashable_json(right))


def test_unpaired_surrogate_is_a_text_error_when_measuring() -> None:
    for value in ({"x": "\ud800"}, {"\udfff": 1}):
        with pytest.raises(JSONLimitError) as excinfo:
            freeze_json(value, path="state", limits=JSONLimits(max_encoded_bytes=1024))
        assert excinfo.value.rule == "text"


def test_unmeasured_freeze_does_not_encode() -> None:
    # Without a byte budget nothing is encoded, so contracts that never
    # measured size keep accepting what they accepted before.
    frozen = freeze_json({"x": "\ud800"}, path="metadata")
    assert frozen.value["x"] == "\ud800"  # type: ignore[index]
    assert frozen.encoded_bytes is None


def test_integer_past_the_str_digit_limit_is_a_number_error() -> None:
    with pytest.raises(JSONLimitError) as excinfo:
        freeze_json({"n": 10**5000}, path="state", limits=JSONLimits(max_encoded_bytes=1 << 20))
    assert excinfo.value.rule == "number"
