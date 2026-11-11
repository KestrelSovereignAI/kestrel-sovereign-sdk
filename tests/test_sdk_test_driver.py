"""Original diagnostic sink survives pytest and retires with its process (#118)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("test_fails", [False, True])
def test_actual_driver_keeps_diagnostics_through_original_interpreter_exit(
    tmp_path: Path, test_fails: bool,
) -> None:
    driver = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "run_sdk_tests.py"
    completed = tmp_path / "original-finalizer-completed"
    invocation = tmp_path / "test_original_driver_invocation.py"
    invocation.write_text(
        "import atexit\n"
        "import faulthandler\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        "def test_original_invocation():\n"
        "    sink = vars(sys.modules['__main__'])['_stderr']\n"
        "    assert type(sink) is int, 'diagnostics require the original process-owned FD, not a GC-owned TextIO'\n"
        "    assert os.get_inheritable(sink) is False\n"
        "    os.fstat(sink)\n"
        "    original_fd = sink\n"
        "    del sink\n"
        "    def _late_original_native_diagnostics():\n"
        "        assert faulthandler.is_enabled()\n"
        "        faulthandler.dump_traceback(file=original_fd, all_threads=True)\n"
        f"        Path({str(completed)!r}).write_text('original native finalizer completed')\n"
        "    atexit.register(_late_original_native_diagnostics)\n"
        f"    assert not {test_fails!r}\n",
    )
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_CURRENT_TEST", None)
    original = subprocess.run(
        [
            sys.executable, "-B", "-W", "error::ResourceWarning", str(driver),
            str(invocation), "-q", "-o", "addopts=", "-p", "no:cacheprovider",
            str(driver.parents[2] / "tests" / "test_host_control_errors.py"),
            str(driver.parents[2] / "tests" / "native_a2a" / "utils" / "test_error_handlers.py"),
            f"--rootdir={tmp_path}", f"--confcutdir={tmp_path}",
        ],
        cwd=tmp_path, env=environment, capture_output=True, text=True,
        timeout=30, check=False,
    )
    assert original.returncode == int(test_fails), (original.stdout, original.stderr)
    assert f"SDK pytest.main returned exit code {int(test_fails)}" in original.stdout
    # This executes during the actual original interpreter's atexit, not a
    # substitute pytest receipt or an unjoined replacement process.
    assert completed.read_text() == "original native finalizer completed"
    assert "_late_original_native_diagnostics" in original.stderr
    assert "ResourceWarning" not in original.stderr
    assert "Exception ignored" not in original.stderr
