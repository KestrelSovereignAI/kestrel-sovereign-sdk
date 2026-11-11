"""Real disposable fixture commits retain required identity and Git failures."""

from pathlib import Path
import subprocess

import pytest

from tests.git_support import commit_fixture


@pytest.mark.parametrize("checked", [False, True])
def test_fixture_commit_failure_is_not_a_false_commit(tmp_path: Path, checked: bool) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    arguments = (tmp_path, "--invalid-fixture-commit-option")
    if checked:
        with pytest.raises(subprocess.CalledProcessError) as captured:
            commit_fixture(*arguments)
        assert captured.value.returncode != 0
        assert "invalid-fixture-commit-option" in captured.value.stderr
    else:
        failed = commit_fixture(*arguments, check=False)
        assert failed.returncode != 0
        assert "invalid-fixture-commit-option" in failed.stderr
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--verify", "HEAD"],
        check=False, capture_output=True, text=True,
    )
    assert head.returncode != 0
    assert subprocess.check_output(
        ["git", "-C", str(tmp_path), "config", "user.email"], text=True,
    ).strip() == "unclesaurus@proton.me"
