"""One bounded deep-freeze for JSON-shaped values in public SDK contracts.

Several contracts accept caller-supplied JSON-shaped data and must keep an
immutable copy of it: operator artifact metadata, inference-lease public
metadata, and decision request state. Each has its own domain policy (which
keys are allowed, how strings are vetted, numeric precision), but the walk
itself — type checks, cycle and depth bounds, node and key budgets, and the
encoded-size budget — is one rule. It lives here so the three contracts cannot
drift on it.

The walk is depth-bounded and tracks the containers on the current path, so a
self-referencing value fails with :class:`JSONLimitError` (``rule="cycle"``)
instead of a ``RecursionError``. When ``max_encoded_bytes`` is set, the running
size of the value's canonical JSON encoding (UTF-8, compact separators, no
ASCII escaping) is accumulated during the same walk and the walk stops as soon
as it passes the budget, so an oversized value is rejected after at most about
the budget's worth of encoding work.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeAlias

JSONScalar: TypeAlias = None | bool | int | float | str
ImmutableJSON: TypeAlias = JSONScalar | tuple["ImmutableJSON", ...] | Mapping[
    str, "ImmutableJSON"
]

KeyValidator: TypeAlias = Callable[[object, str], str]
StringValidator: TypeAlias = Callable[[str, str], str]
NumberValidator: TypeAlias = Callable[[int | float, str], int | float]


class JSONTypeError(TypeError):
    """A value or key is not part of the accepted JSON value tree."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


class JSONLimitError(ValueError):
    """A value is JSON-shaped but violates a bound or a value rule."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


@dataclass(frozen=True, slots=True)
class JSONLimits:
    """Bounds for one freeze. ``None`` means the bound is not enforced.

    ``max_depth`` counts nesting below the root: the root container is depth 0
    and its direct children are depth 1.
    """

    max_depth: int | None = None
    max_nodes: int | None = None
    max_keys: int | None = None
    max_encoded_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class FrozenJSON:
    """A frozen value and the byte length of its canonical JSON encoding.

    ``encoded_bytes`` is only computed when the freeze ran with a
    ``max_encoded_bytes`` budget; otherwise it is ``None``.
    """

    value: ImmutableJSON
    encoded_bytes: int | None


def _require_str_key(key: object, path: str) -> str:
    if not isinstance(key, str):
        raise JSONTypeError("key", f"{path} keys must be strings")
    return key


class _Walk:
    def __init__(
        self,
        *,
        path: str,
        limits: JSONLimits,
        validate_key: KeyValidator,
        validate_string: StringValidator | None,
        validate_number: NumberValidator | None,
    ) -> None:
        self.path = path
        self.limits = limits
        self.validate_key = validate_key
        self.validate_string = validate_string
        self.validate_number = validate_number
        self.nodes = 0
        self.keys = 0
        self.size = 0 if limits.max_encoded_bytes is not None else None
        self.ancestors: set[int] = set()

    def add_bytes(self, count: int) -> None:
        if self.size is None:
            return
        self.size += count
        budget = self.limits.max_encoded_bytes
        assert budget is not None
        if self.size > budget:
            raise JSONLimitError(
                "bytes",
                f"{self.path} must not exceed {budget} bytes when encoded as JSON",
            )

    def add_node(self) -> None:
        self.nodes += 1
        limit = self.limits.max_nodes
        if limit is not None and self.nodes > limit:
            raise JSONLimitError(
                "nodes", f"{self.path} must not exceed {limit} nodes"
            )

    def add_key(self) -> None:
        self.keys += 1
        limit = self.limits.max_keys
        if limit is not None and self.keys > limit:
            raise JSONLimitError("keys", f"{self.path} must not exceed {limit} keys")

    def encoded_scalar_bytes(self, value: JSONScalar) -> int:
        return len(
            json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )

    def freeze(self, value: Any, depth: int) -> ImmutableJSON:
        limit = self.limits.max_depth
        if limit is not None and depth > limit:
            raise JSONLimitError(
                "depth", f"{self.path} must not exceed {limit} levels"
            )
        self.add_node()
        if value is None or isinstance(value, bool):
            self.add_bytes(self.encoded_scalar_bytes(value))
            return value
        if isinstance(value, str):
            if self.validate_string is not None:
                value = self.validate_string(value, self.path)
            self.add_bytes(self.encoded_scalar_bytes(value))
            return value
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise JSONLimitError(
                    "number", f"{self.path} cannot contain non-finite numbers"
                )
            if self.validate_number is not None:
                value = self.validate_number(value, self.path)
            self.add_bytes(self.encoded_scalar_bytes(value))
            return value
        if isinstance(value, Mapping):
            return self._freeze_mapping(value, depth)
        if isinstance(value, (list, tuple)):
            return self._freeze_sequence(value, depth)
        raise JSONTypeError(
            "type",
            f"{self.path} values must be JSON-like (scalar, list or object), "
            f"not {type(value).__name__}",
        )

    def _enter(self, container: object) -> None:
        marker = id(container)
        if marker in self.ancestors:
            raise JSONLimitError("cycle", f"{self.path} must not contain a cycle")
        self.ancestors.add(marker)

    def _freeze_mapping(self, value: Mapping[Any, Any], depth: int) -> ImmutableJSON:
        self._enter(value)
        try:
            frozen: dict[str, ImmutableJSON] = {}
            self.add_bytes(2)  # {}
            for index, (raw_key, item) in enumerate(value.items()):
                key = self.validate_key(raw_key, self.path)
                self.add_key()
                # key, colon, and the comma before every entry but the first
                self.add_bytes(self.encoded_scalar_bytes(key) + 1 + (1 if index else 0))
                frozen[key] = self.freeze(item, depth + 1)
            return MappingProxyType(frozen)
        finally:
            self.ancestors.discard(id(value))

    def _freeze_sequence(
        self, value: list[Any] | tuple[Any, ...], depth: int
    ) -> ImmutableJSON:
        self._enter(value)
        try:
            self.add_bytes(2)  # []
            items: list[ImmutableJSON] = []
            for index, item in enumerate(value):
                if index:
                    self.add_bytes(1)  # comma
                items.append(self.freeze(item, depth + 1))
            return tuple(items)
        finally:
            self.ancestors.discard(id(value))


def freeze_json(
    value: Any,
    *,
    path: str,
    limits: JSONLimits = JSONLimits(),
    validate_key: KeyValidator | None = None,
    validate_string: StringValidator | None = None,
    validate_number: NumberValidator | None = None,
) -> FrozenJSON:
    """Deep-freeze a JSON-shaped value under ``limits`` and domain validators.

    Mappings become read-only :class:`types.MappingProxyType` over a fresh
    ``dict``; lists and tuples become tuples. Accepted scalars are ``None``,
    ``bool``, ``int``, finite ``float`` and ``str``. Keys must be strings
    unless ``validate_key`` says otherwise (it receives the raw key and the
    path and returns the key to store, raising to reject).

    Raises :class:`JSONTypeError` for values or keys outside the JSON tree and
    :class:`JSONLimitError` for bounds, cycles and non-finite numbers. Domain
    validators raise their own ``TypeError``/``ValueError``.
    """

    walk = _Walk(
        path=path,
        limits=limits,
        validate_key=validate_key or _require_str_key,
        validate_string=validate_string,
        validate_number=validate_number,
    )
    frozen = walk.freeze(value, 0)
    return FrozenJSON(value=frozen, encoded_bytes=walk.size)


def thaw_json(value: ImmutableJSON) -> Any:
    """Return a plain ``dict``/``list`` copy of a frozen value."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def hashable_json(value: ImmutableJSON) -> object:
    """Return an equality-consistent hashable representation of frozen JSON."""

    if isinstance(value, Mapping):
        return frozenset((key, hashable_json(item)) for key, item in value.items())
    if isinstance(value, tuple):
        return tuple(hashable_json(item) for item in value)
    return value


def canonical_json_bytes(value: ImmutableJSON) -> bytes:
    """Encode a frozen value exactly as :func:`freeze_json` measured it."""

    return json.dumps(
        thaw_json(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
