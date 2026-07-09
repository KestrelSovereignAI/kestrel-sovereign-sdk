"""SDK-owned UI contribution shape.

Feature packages (both agent-scoped ``Feature`` and host-scoped
``HostFeature``) ship optional console panels by returning a
``UIContributions``: a static asset directory plus the ES module entry
points sovereign should register, optionally gated behind a capability.

This is *pure data* — it imports nothing from feature packages or the
framework, so it is safe to own in the dependency-free SDK. It replaces the
local fallback copies feature packages currently hand-roll. ``Feature`` can
adopt it later without a breaking change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class UIContributions:
    """Console UI assets a feature contributes to the host console.

    Attributes:
        static_dir: Absolute (or package-relative) filesystem path to the
            directory of static assets sovereign should mount/serve. ``None``
            when the feature ships no static bundle.
        modules: ES module entry points (URLs or mount-relative paths) the
            console should load to register the feature's panels. Empty when
            the feature contributes no client modules.
        capability: Optional capability slug gating whether this UI is shown.
            ``None`` means the UI is always available; a value means the host
            should only surface it to principals holding that capability.
    """

    static_dir: Optional[str] = None
    modules: List[str] = field(default_factory=list)
    capability: Optional[str] = None


__all__ = ["UIContributions"]
