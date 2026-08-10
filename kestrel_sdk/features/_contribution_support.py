"""Lightweight helpers shared by the feature base contracts."""

from __future__ import annotations

import hashlib
import re
from importlib import import_module

from kestrel_sdk._validation import stable_token


_UNSAFE_IDENTITY_CHARACTERS = re.compile(r"[^A-Za-z0-9._:@-]+")
_IDENTITY_EDGE_CHARACTERS = ".-_:@"


def contribution_annotation(name: str) -> object:
    """Resolve one contribution type only when annotations are inspected."""

    return getattr(import_module("kestrel_sdk.features.contributions"), name)


def implementation_contribution_owner(implementation: type[object]) -> str:
    """Return a stable, bounded module-qualified implementation identity."""

    raw_identity = f"{implementation.__module__}:{implementation.__qualname__}"
    try:
        return stable_token(raw_identity, "feature contribution owner")
    except ValueError:
        pass

    digest = hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()[:16]
    readable = _UNSAFE_IDENTITY_CHARACTERS.sub("-", raw_identity)
    readable = readable.strip(_IDENTITY_EDGE_CHARACTERS) or "feature"
    suffix = f"@{digest}"
    readable = readable[: 256 - len(suffix)].rstrip(_IDENTITY_EDGE_CHARACTERS)
    return stable_token(
        f"{readable or 'feature'}{suffix}", "feature contribution owner"
    )


__all__ = ["contribution_annotation", "implementation_contribution_owner"]
