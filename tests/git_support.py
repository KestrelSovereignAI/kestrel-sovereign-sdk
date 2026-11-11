"""One required identity for real, disposable Git fixture commits."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


def commit_fixture(
    checkout: Path, *arguments: str, check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Commit in a test-owned repository after verifying its actual identity."""
    required = {"user.name": "UncleSaurus", "user.email": "unclesaurus@proton.me"}
    for option, value in required.items():
        subprocess.run(
            ["git", "-C", str(checkout), "config", "--local", option, value],
            check=True, capture_output=True, text=True,
        )
        actual = subprocess.check_output(
            ["git", "-C", str(checkout), "config", option], text=True,
        ).strip()
        assert actual == value
    fixture_environment = dict(os.environ)
    for role in ("AUTHOR", "COMMITTER"):
        for field in ("NAME", "EMAIL"):
            fixture_environment.pop(f"GIT_{role}_{field}", None)
    original = subprocess.run(
        ["git", "-C", str(checkout), "-c", "core.hooksPath=/dev/null", "commit", *arguments],
        check=check, capture_output=True, text=True, env=fixture_environment,
    )
    if original.returncode == 0:
        identity = subprocess.check_output(
            ["git", "-C", str(checkout), "log", "-1", "--format=%an <%ae>%n%cn <%ce>"],
            text=True,
        ).splitlines()
        expected = f"{required['user.name']} <{required['user.email']}>"
        assert identity == [expected, expected]
    return original
