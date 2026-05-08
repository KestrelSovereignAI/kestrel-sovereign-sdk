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
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from enum import Enum


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
    """

    url: str
    persistent: bool
    description: str


_VOLATILE = {PrivacyMode.EPHEMERAL, PrivacyMode.ISOLATED}


def resolve_engine_target(mode: PrivacyMode, fallback_url: str) -> EngineTarget:
    """Return the engine target for ``mode``.

    Volatile modes ignore ``fallback_url`` and return process-local
    storage (in-memory or a tempfile). Persistent modes pass
    ``fallback_url`` through unchanged — caller chose it.

    Args:
        mode: Active privacy mode.
        fallback_url: SQLAlchemy URL to use when the mode is persistent.
            Required for persistent modes; ignored for volatile ones.

    Returns:
        An `EngineTarget` carrying the resolved URL, durability flag,
        and a short human-readable description.

    Raises:
        ValueError: If ``mode`` is persistent but ``fallback_url`` is
            empty/None — the caller forgot to plumb the configured URL
            through.
    """
    if mode == PrivacyMode.EPHEMERAL:
        return EngineTarget(
            url="sqlite+aiosqlite:///:memory:",
            persistent=False,
            description="in-memory (ephemeral)",
        )
    if mode == PrivacyMode.ISOLATED:
        # Tempfile path — caller is responsible for cleanup at session end.
        # NamedTemporaryFile would auto-delete on close; we want the path
        # to outlive this function call, so mkstemp + close the fd.
        fd, path = tempfile.mkstemp(prefix="kestrel-isolated-", suffix=".sqlite")
        os.close(fd)
        return EngineTarget(
            url=f"sqlite+aiosqlite:///{path}",
            persistent=False,
            description=f"tempfile (isolated): {path}",
        )
    if not fallback_url:
        raise ValueError(
            f"resolve_engine_target({mode!r}) requires a non-empty fallback_url"
        )
    return EngineTarget(
        url=fallback_url,
        persistent=True,
        description=f"persistent ({mode.value}): {fallback_url}",
    )


__all__ = ["PrivacyMode", "EngineTarget", "resolve_engine_target"]
