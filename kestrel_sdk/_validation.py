"""Validation helpers shared by public SDK contract modules."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TypeVar


_STABLE_TOKEN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._:@-]{0,254}[A-Za-z0-9])?$"
)
_SEMVER_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_SEMANTIC_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    rf"(?:-({_SEMVER_IDENTIFIER}(?:\.{_SEMVER_IDENTIFIER})*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

_T = TypeVar("_T")

# Unicode's White_Space property, expressed as code points so validation does
# not vary with the Unicode database bundled with the running interpreter.
_NON_ASCII_WHITESPACE = frozenset(
    {
        0x0085,
        0x00A0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
    }
)
_ALLOWED_FORMAT_CHARACTERS = frozenset({0x200C, 0x200D})  # ZWNJ and ZWJ
_UNSAFE_FORMAT_RANGES = (
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)


def stable_token(value: str, field_name: str) -> str:
    """Return a validated, bounded token suitable for a stable identifier."""

    if not isinstance(value, str) or not _STABLE_TOKEN.fullmatch(value):
        raise ValueError(
            f"{field_name} must be a non-empty stable token of at most 256 characters"
        )
    return value


def non_empty_text(value: str, field_name: str) -> str:
    """Return bounded browser-safe text without silently normalizing it."""

    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(
            f"{field_name} must be a non-empty string of at most 256 characters"
        )
    if value != value.strip():
        raise ValueError(f"{field_name} must not have leading or trailing whitespace")
    browser_safe_string(value, field_name, max_length=256)
    return value


def browser_safe_string(
    value: str,
    field_name: str,
    *,
    max_length: int,
    allow_empty: bool = True,
) -> str:
    """Validate text that may cross a browser or JSON boundary."""

    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if (not allow_empty and not value) or len(value) > max_length:
        qualifier = "a non-empty " if not allow_empty else "a "
        raise ValueError(
            f"{field_name} must be {qualifier}string of at most {max_length} characters"
        )
    for character in value:
        codepoint = ord(character)
        if _unsafe_browser_codepoint(codepoint):
            raise ValueError(f"{field_name} contains unsafe text characters")
    return value


def _unsafe_browser_codepoint(codepoint: int) -> bool:
    """Return whether a code point is deterministically unsafe at a UI boundary."""

    if codepoint in _ALLOWED_FORMAT_CHARACTERS:
        return False
    if (
        codepoint < 0x20
        or 0x7F <= codepoint <= 0x9F
        or codepoint in _NON_ASCII_WHITESPACE
        or 0xD800 <= codepoint <= 0xDFFF
        or 0xE000 <= codepoint <= 0xF8FF
        or 0xF0000 <= codepoint <= 0xFFFFD
        or 0x100000 <= codepoint <= 0x10FFFD
        or 0xFDD0 <= codepoint <= 0xFDEF
        or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}
    ):
        return True
    return any(start <= codepoint <= end for start, end in _UNSAFE_FORMAT_RANGES)


def semantic_version(
    value: str,
    field_name: str = "version",
    *,
    allow_prerelease: bool = True,
    allow_build: bool = False,
) -> str:
    """Validate a SemVer 2 compatible version string."""

    match = (
        _SEMANTIC_VERSION.fullmatch(value)
        if isinstance(value, str) and len(value) <= 128
        else None
    )
    if (
        not isinstance(value, str)
        or match is None
        or (not allow_prerelease and match.group(4) is not None)
        or (not allow_build and match.group(5) is not None)
    ):
        raise ValueError(f"{field_name} must be a semantic version such as '1.0.0'")
    return value


def semantic_version_parts(value: str) -> tuple[int, int, int]:
    """Return the numeric release tuple for an already validated version."""

    match = _SEMANTIC_VERSION.fullmatch(value)
    if match is None:  # pragma: no cover - callers validate first
        raise ValueError("invalid semantic version")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def frozen_tokens(values: Iterable[str], field_name: str) -> frozenset[str]:
    """Normalize an iterable of identifiers to an immutable set."""

    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise TypeError(f"{field_name} must be an iterable of stable tokens")
    return frozenset(stable_token(value, field_name) for value in values)


def unique_tuple(values: Iterable[_T], field_name: str) -> tuple[_T, ...]:
    """Normalize a collection to a tuple while rejecting duplicate entries."""

    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise TypeError(f"{field_name} must be an iterable")
    normalized = tuple(values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized
