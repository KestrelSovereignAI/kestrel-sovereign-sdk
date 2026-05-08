"""Privacy → engine target mapping for feature ORM packages.

The framework's full `PrivacyConfig` (storage, llm_location, shareable,
computer_access) lives in `kestrel_sovereign.privacy` and stays sovereign-
private. The SDK exposes only the 5-mode enum and a deterministic mapping
from mode → SQLAlchemy URL so feature packages (e.g. `kestrel-feature-
entities`) can bind their ORM engine to the same target the agent uses.

Mapping:

  EPHEMERAL  → sqlite+aiosqlite:///:memory:                    (volatile)
  ISOLATED   → sqlite+aiosqlite:///<tempfile>                  (volatile)
  ANONYMOUS  → fallback_url                                    (persistent)
  NORMAL     → fallback_url                                    (persistent)
  PUBLIC     → fallback_url                                    (persistent)

Volatile modes intentionally ignore `fallback_url` — that is the entire
point of EPHEMERAL/ISOLATED. A feature package that calls
`resolve_engine_target(EPHEMERAL, "postgresql://prod")` gets back
``sqlite+aiosqlite:///:memory:`` and a `persistent=False` flag, which it
should respect (skip migrations that assume durability, etc.).

`PrivacyMode` is `str`-based so equality with raw strings holds
(`PrivacyMode.NORMAL == "normal"` is True, hashes match) — feature
packages can read modes from TOML/JSON/CLI flags without explicit
coercion. Code that previously relied on `isinstance(x, PrivacyMode)`
discriminating against plain strings will need to switch to
`type(x) is PrivacyMode`.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union


class PrivacyMode(str, Enum):
    """5-mode privacy enum shared by sovereign and feature packages.

    Inherits from `str` so values round-trip through TOML/JSON/CLI flags
    without explicit conversion (``PrivacyMode("normal")`` works, and
    ``mode == "normal"`` is true).
    """

    EPHEMERAL = "ephemeral"
    ISOLATED = "isolated"
    ANONYMOUS = "anonymous"
    NORMAL = "normal"
    PUBLIC = "public"


@dataclass(frozen=True)
class EngineTarget:
    """Resolved DB target for a given privacy mode.

    Attributes:
        url: SQLAlchemy URL the feature package should hand to
            ``create_async_engine`` (or sync equivalent).
        persistent: Whether data written through this engine is expected
            to survive the agent session. Volatile modes set this False
            so feature packages can skip durability-assuming setup
            (e.g. external migration tools, snapshot exporters).
        description: Human-readable label for logs/UI/diagnostics.
        cleanup_path: Backing file the caller should unlink at session
            end. Set only for ISOLATED (which materialises a tempfile);
            ``None`` for every other mode. Use `cleanup()` to remove it
            idempotently — feature packages don't have to parse the URL.
    """

    url: str
    persistent: bool
    description: str
    cleanup_path: Optional[str] = None

    def cleanup(self) -> None:
        """Idempotently remove any backing tempfile this target owns.

        Safe to call repeatedly and on targets that don't own a tempfile
        (no-op when ``cleanup_path is None``). The expected pattern is::

            target = resolve_engine_target(mode, fallback_url)
            try:
                engine = create_async_engine(target.url)
                ...
            finally:
                target.cleanup()
        """
        if self.cleanup_path is None:
            return
        try:
            os.unlink(self.cleanup_path)
        except FileNotFoundError:
            pass


def resolve_engine_target(
    mode: Union[PrivacyMode, str], fallback_url: Optional[str]
) -> EngineTarget:
    """Return the engine target for ``mode``.

    Volatile modes (EPHEMERAL, ISOLATED) ignore ``fallback_url`` and
    return process-local storage (in-memory or a tempfile). Persistent
    modes (ANONYMOUS, NORMAL, PUBLIC) pass ``fallback_url`` through
    unchanged — caller chose it.

    Args:
        mode: Active privacy mode. Accepts a `PrivacyMode` instance or
            its string value (``"normal"``, ``"ephemeral"``, …); strings
            outside the enum raise ``ValueError``.
        fallback_url: SQLAlchemy URL to use when the mode is persistent.
            Required (non-empty, non-whitespace) for persistent modes;
            ignored for volatile ones — pass ``None`` if you don't have
            one yet.

    Returns:
        An `EngineTarget` carrying the resolved URL, durability flag,
        description, and (for ISOLATED) a `cleanup_path` the caller
        should pass to `EngineTarget.cleanup()` at session end.

    Raises:
        ValueError: If ``mode`` is not a recognised privacy mode, or if
            the mode is persistent but ``fallback_url`` is empty,
            whitespace-only, or ``None`` — the caller forgot to plumb the
            configured URL through.
    """
    if not isinstance(mode, PrivacyMode):
        # Coerce strings (or pre-#1094 sovereign enum members whose values
        # are still "ephemeral"/"normal"/etc.) to the canonical SDK enum.
        # Strip the .value off any unrelated Enum first, since
        # `PrivacyMode(SomeOtherEnum.NORMAL)` would otherwise compare the
        # member object (not its string value) and raise ValueError.
        if isinstance(mode, Enum):
            mode = mode.value
        mode = PrivacyMode(mode)

    if mode == PrivacyMode.EPHEMERAL:
        return EngineTarget(
            url="sqlite+aiosqlite:///:memory:",
            persistent=False,
            description="in-memory (ephemeral)",
        )
    if mode == PrivacyMode.ISOLATED:
        # Caller is responsible for cleanup at session end via
        # `EngineTarget.cleanup()`. We use mkstemp + close-fd rather
        # than NamedTemporaryFile so the path outlives this function
        # call without depending on Python finalisation order.
        fd, path = tempfile.mkstemp(prefix="kestrel-isolated-", suffix=".sqlite")
        os.close(fd)
        return EngineTarget(
            url=f"sqlite+aiosqlite:///{path}",
            persistent=False,
            description=f"tempfile (isolated): {path}",
            cleanup_path=path,
        )

    if fallback_url is None or not fallback_url.strip():
        raise ValueError(
            f"resolve_engine_target({mode!r}) requires a non-empty fallback_url; "
            f"got {fallback_url!r}"
        )
    return EngineTarget(
        url=fallback_url,
        persistent=True,
        description=f"persistent ({mode.value}): {fallback_url}",
    )


__all__ = ["PrivacyMode", "EngineTarget", "resolve_engine_target"]
