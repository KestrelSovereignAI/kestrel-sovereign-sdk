"""Offline custody verification for the packaged modified A2A SDK.

The manifest is build provenance, not an independent signature or trust root.
Source review and publisher/artifact verification establish its authority.
"""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_vendored_a2a(root: Path | None = None) -> list[str]:
    """Verify complete file custody and reversible, module-only relocation."""
    destination = (root / "kestrel_sdk" / "native_a2a") if root is not None else Path(files("kestrel_sdk.native_a2a"))
    manifest = json.loads((destination / "VENDORED.json").read_text())
    failures = []
    expected = set(manifest["files"]) | set(manifest["provenance_sha256"]) | {"VENDORED.json"}
    actual = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if actual != expected:
        failures.append(f"Vendored file set differs: missing={expected - actual}, extra={actual - expected}")
    for name, record in manifest["files"].items():
        path = destination / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if _sha256(data) != record["vendored_sha256"]:
            failures.append(f"Vendored source digest differs: {name}")
        text = data.decode("utf-8")
        if path.suffix == ".py":
            text = text.removeprefix("# Modified by Kestrel: isolated SDK namespace; see VENDORED.json.\n")
        original = text.replace("kestrel_sdk.native_a2a", "a2a").encode("utf-8")
        if _sha256(original) != record["upstream_sha256"]:
            failures.append(f"Namespace relocation does not reproduce reviewed source: {name}")
    for name, digest in manifest["provenance_sha256"].items():
        path = destination / name
        if path.is_file() and _sha256(path.read_bytes()) != digest:
            failures.append(f"Vendored provenance digest differs: {name}")
    return failures
