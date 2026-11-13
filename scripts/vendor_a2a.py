"""Import and verify the reviewed A2A SDK in Kestrel's isolated namespace.

The input is an explicit Git tree, never an installed package or a moving branch.
UPSTREAM.patch reconstructs the reviewed SDK and its regression tests from the
recorded public base. Relocation changes Python module paths, not protocol names,
protobuf descriptors, JSON fields, routes, or cryptographic algorithms.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path


def relocate(text: str) -> str:
    """Relocate module references without changing qualified protobuf names."""
    relocated = re.sub(
        r"(?<![\w./])a2a\.(?=(?:_base|auth|client|compat|extensions|helpers|migrations|plugins|server|types|utils|a2a_db_cli)\b)",
        "kestrel_sdk.native_a2a.",
        text,
    )
    relocated = re.sub(r"\bfrom a2a(?=\s+import\b)", "from kestrel_sdk.native_a2a", relocated)
    return re.sub(r"\bfiles\((['\"])a2a\1\)", "files('kestrel_sdk.native_a2a')", relocated)


def relocate_test(text: str) -> str:
    """Relocate a source regression and its shared original-resource witness."""
    relocated = relocate(text).replace(
        "from tests.source_witness import ",
        "from tests.native_a2a.source_witness import ",
    )
    return (
        "# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.\n"
        + relocated
    )


def import_test_delta(
    root: Path, source: Path, base: str, revision: str,
    *, retain_tests_from: Path | None = None,
) -> None:
    """Import changed tests plus the existing reviewed corpus from one Git tree.

    The source is an exact Git tree, never dirty tests or every upstream fixture.
    A fresh output is required. Deletion/rename disposition must be explicit.
    """
    destination = root / "tests" / "native_a2a"
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite relocated tests: {destination}")
    tree = _git(source, "rev-parse", revision + "^{tree}").decode().strip()
    records = _git(source, "diff", "--name-status", "--no-renames", "-z", base, tree, "--", "tests").decode().split("\0")
    selected: set[str] = set()
    for status, path in zip(records[0:-1:2], records[1::2], strict=True):
        if status not in {"A", "M"} or not path.endswith(".py"):
            raise ValueError(f"Test delta needs explicit disposition: {status} {path}")
        selected.add(path)
    available = set(_git(source, "ls-tree", "-rz", "--name-only", tree, "tests").decode().split("\0"))
    if retain_tests_from is not None:
        if not retain_tests_from.is_dir() or retain_tests_from.is_symlink():
            raise ValueError("The retained test corpus must be an actual directory")
        previous = tuple(retain_tests_from.rglob("*.py"))
        if not previous or any(path.is_symlink() for path in previous):
            raise ValueError("The retained test corpus must contain regular Python files")
        retained = {
            "tests/" + path.relative_to(retain_tests_from).as_posix()
            for path in previous
        }
        if missing := retained - available:
            raise ValueError(f"Retained tests need explicit disposition: {sorted(missing)}")
        # Only paths are retained. Every byte, including unchanged regressions,
        # is regenerated from the exact producer tree, never the prior checkout.
        selected.update(retained)
    for path in tuple(selected):
        for parent in Path(path).parents:
            if parent == Path("."):
                break
            package = (parent / "__init__.py").as_posix()
            if package in available:
                selected.add(package)
    contents = {
        Path(path).relative_to("tests"): relocate_test(_git(source, "show", f"{tree}:{path}").decode())
        for path in sorted(selected)
    }
    for relative, text in contents.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(source: Path, *arguments: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(source), *arguments])


def import_vendor(root: Path, source: Path, revision: str, base: str) -> None:
    """Materialize a pinned, licensed snapshot with reversible relocation."""
    tree = _git(source, "rev-parse", revision + "^{tree}").decode().strip()
    base_commit = _git(source, "rev-parse", base + "^{commit}").decode().strip()
    # The nearest upstream release tag; a base past it reads 1.3.0-12-g11f1116.
    base_release = _git(source, "describe", "--tags", "--always", base_commit).decode().strip().removeprefix("v")
    destination = root / "kestrel_sdk" / "native_a2a"
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite vendored source: {destination}")
    paths = _git(source, "ls-tree", "-rz", "--name-only", tree, "src/a2a")
    files = {}
    contents = {}
    for raw_path in paths.decode().split("\0"):
        if not raw_path:
            continue
        relative = Path(raw_path).relative_to("src/a2a")
        original = _git(source, "show", f"{tree}:{raw_path}")
        text = original.decode("utf-8")
        if "kestrel_sdk.native_a2a." in text:
            raise ValueError(f"Source already contains the vendor namespace: {raw_path}")
        relocated_text = relocate(text)
        modified = relocated_text != text
        if relative.suffix == ".py":
            for node in ast.walk(ast.parse(relocated_text)):
                if isinstance(node, ast.ImportFrom) and node.module and (node.module == "a2a" or node.module.startswith("a2a.")):
                    raise ValueError(f"Unrelocated SDK import in {raw_path}: {node.module}")
                if isinstance(node, ast.Import) and any(alias.name == "a2a" or alias.name.startswith("a2a.") for alias in node.names):
                    raise ValueError(f"Unrelocated SDK import in {raw_path}")
            relocated_text = "# Modified by Kestrel: isolated SDK namespace; see VENDORED.json.\n" + relocated_text
        relocated = relocated_text.encode("utf-8")
        contents[relative.as_posix()] = relocated
        files[relative.as_posix()] = {
            "upstream_sha256": _digest(original),
            "vendored_sha256": _digest(relocated),
            "namespace_modified": modified,
        }
    provenance = {}
    for name, original_path in [("LICENSE", "LICENSE"), ("UPSTREAM-PYPROJECT.toml", "pyproject.toml")]:
        data = _git(source, "show", f"{tree}:{original_path}")
        contents[name] = data
        provenance[name] = _digest(data)
    patch = _git(source, "diff", "--binary", base_commit, tree, "--", "src/a2a", "tests")
    contents["UPSTREAM.patch"] = patch
    provenance["UPSTREAM.patch"] = _digest(patch)
    notice = (
        b"A2A Python SDK\nCopyright Google LLC and A2A Python SDK contributors.\n"
        b"Licensed under the Apache License, Version 2.0; see LICENSE.\n\n"
        b"This copy is modified and maintained by Kestrel. It is not an official\n"
        b"A2A Python SDK release. VENDORED.json identifies the source and file digests;\n"
        b"UPSTREAM.patch records the functional changes and their regression tests.\n"
        b"Python imports are relocated into kestrel_sdk.native_a2a.\n"
        b"The Agent2Agent wire protocol remains A2A 1.0.1.\n"
    )
    contents["NOTICE"] = notice
    provenance["NOTICE"] = _digest(notice)
    manifest = {
        "repository": "https://github.com/a2aproject/a2a-python",
        "protocol": "1.0.1",
        "base_release": base_release,
        "base_commit": base_commit,
        "reviewed_source_tree": tree,
        "namespace": "kestrel_sdk.native_a2a",
        "modifications": "Reviewed signing, HTTP authorization and version negotiation, client ownership, discovery/response decoding, native-only client selection, HTTP error media types, original host-control errors, shared original-source handoff/retirement body, explicit complete HTTP/ASGI request roots, terminal protocol-refusal data after actual source retirement, and compatibility gRPC coroutine writers in UPSTREAM.patch; Python module references relocated by scripts/vendor_a2a.py. This is a modified upstream SDK, not an official release.",
        "files": files,
        "provenance_sha256": provenance,
    }
    contents["VENDORED.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".a2a-import-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "a2a"
        for name, data in contents.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        staging.rename(destination)


def check_vendor(root: Path) -> list[str]:
    """Use the same custody verifier as installed artifact acceptance."""
    from kestrel_sdk.native_a2a_custody import verify_vendored_a2a

    return verify_vendored_a2a(root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--base")
    parser.add_argument("--tests-since", help="Exact prior producer revision; import its changed tests into a fresh output")
    parser.add_argument("--retain-tests-from", type=Path, help="Existing reviewed tests/native_a2a corpus; regenerate these paths from the producer too")
    arguments = parser.parse_args()
    if arguments.retain_tests_from is not None and (arguments.check or arguments.tests_since is None):
        parser.error("--retain-tests-from requires an import with --tests-since")
    if not arguments.check:
        if not all((arguments.source, arguments.revision, arguments.base)):
            parser.error("Import requires --source, --revision and --base")
        import_vendor(arguments.root, arguments.source, arguments.revision, arguments.base)
        if arguments.tests_since is not None:
            import_test_delta(arguments.root, arguments.source, arguments.tests_since, arguments.revision, retain_tests_from=arguments.retain_tests_from)
    failures = check_vendor(arguments.root)
    if failures:
        parser.exit(1, "\n".join(failures) + "\n")


if __name__ == "__main__":
    main()
