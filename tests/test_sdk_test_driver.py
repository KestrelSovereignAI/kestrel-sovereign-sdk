"""Original diagnostic sink survives pytest and retires with its process (#118)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


def _run_diagnostic_child(
    tmp_path: Path, source: str, *driver_arguments: str,
    active_interval: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Exercise the actual driver/native watchdog in a minimal real pytest child."""
    driver = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "run_sdk_tests.py"
    if active_interval is not None:
        # Keep the real CLI's normal startup budget through cold imports and
        # collection. Configure this SAME native controller for accelerated
        # active-work tests; only the actual pytest progress hooks rearm it.
        # No replacement timer/process or manufactured progress notification.
        source = (
            "import sys\n"
            f"vars(sys.modules['__main__'])['_diagnostics']._interval = {active_interval!r}\n"
            + source
        )
    invocation = tmp_path / "test_diagnostic_invocation.py"
    invocation.write_text(source)
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_CURRENT_TEST", None)
    # This child tests the driver, not arbitrary installed third-party plugins.
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    original = subprocess.run(
        [sys.executable, "-B", "-W", "error::ResourceWarning", str(driver),
         *driver_arguments, str(invocation), "-q", "-s", "-o", "addopts=",
         "-p", "no:cacheprovider", f"--rootdir={tmp_path}", f"--confcutdir={tmp_path}"],
        cwd=tmp_path, env=environment, capture_output=True, text=True,
        timeout=30, check=False,
    )
    (tmp_path / "diagnostic-original.stdout").write_text(original.stdout)
    (tmp_path / "diagnostic-original.stderr").write_text(original.stderr)
    (tmp_path / "diagnostic-original.exit").write_text(str(original.returncode))
    return original


def test_accelerated_profile_keeps_actual_collection_on_normal_startup_budget(tmp_path: Path) -> None:
    original = _run_diagnostic_child(tmp_path, '''
import os
import sys
import time

descriptor = vars(sys.modules["__main__"])["_stderr"]
os.write(descriptor, b"ORIGINAL_COLLECTION_BEGIN\\n")
time.sleep(1.1)
os.write(descriptor, b"ORIGINAL_COLLECTION_END\\n")

def test_original():
    pass
''', active_interval=0.5)
    assert original.returncode == 0, (original.stdout, original.stderr)
    collection = original.stderr.split("ORIGINAL_COLLECTION_BEGIN\n", 1)[1].split("ORIGINAL_COLLECTION_END\n", 1)[0]
    assert "Timeout (" not in collection, "The active-work test budget leaked into collection"
    assert "SDK pytest.main returned exit code 0" in original.stdout


@pytest.mark.parametrize("test_fails", [False, True])
def test_progress_longer_than_diagnostic_interval_does_not_dump_healthy_frames(
    tmp_path: Path, test_fails: bool,
) -> None:
    finalized = tmp_path / "healthy-original-finalized"
    original = _run_diagnostic_child(tmp_path, f'''
import atexit
import os
from pathlib import Path
import sys
import time
import pytest

atexit.register(lambda: Path({str(finalized)!r}).write_text("original exited"))

@pytest.mark.parametrize("case", range(8))
def test_healthy_original_progress(case):
    descriptor = vars(sys.modules["__main__"])["_stderr"]
    if case == 0:
        os.write(descriptor, b"HEALTHY_BEGIN\\n")
    time.sleep(0.15)
    if case == 7:
        os.write(descriptor, b"HEALTHY_END\\n")
        assert not {test_fails!r}
''', active_interval=0.5)
    assert original.returncode == int(test_fails), (original.stdout, original.stderr)
    assert finalized.read_text() == "original exited"
    healthy = original.stderr.split("HEALTHY_BEGIN\n", 1)[1].split("HEALTHY_END\n", 1)[0]
    assert "Timeout (" not in healthy, "The watchdog dumped frames despite actual test progress"
    assert f"SDK pytest.main returned exit code {int(test_fails)}" in original.stdout


@pytest.mark.parametrize("phase", ["body", "teardown", "thread-join", "shutdown"])
def test_inactivity_diagnostics_survive_test_teardown_and_original_shutdown(
    tmp_path: Path, phase: str,
) -> None:
    finalized = tmp_path / "stalled-original-finalized"
    original = _run_diagnostic_child(tmp_path, f'''
import atexit
import os
from pathlib import Path
import sys
import threading
import time
import pytest

started = threading.Event()

def stall_original_phase():
    descriptor = vars(sys.modules["__main__"])["_stderr"]
    os.write(descriptor, b"ORIGINAL_STALL_BEGIN\\n")
    started.set()
    time.sleep(1.1)
    os.write(descriptor, b"ORIGINAL_STALL_END\\n")

def finalize_original():
    if {phase!r} == "shutdown":
        stall_original_phase()
    Path({str(finalized)!r}).write_text("original finalized")

atexit.register(finalize_original)

@pytest.fixture
def original_fixture():
    yield
    if {phase!r} == "teardown":
        stall_original_phase()

def test_original(original_fixture):
    if {phase!r} == "body":
        stall_original_phase()
    elif {phase!r} == "thread-join":
        threading.Thread(target=stall_original_phase).start()
        assert started.wait(5)
''', active_interval=0.5)
    assert original.returncode == 0, (original.stdout, original.stderr)
    assert finalized.read_text() == "original finalized"
    stalled = original.stderr.split("ORIGINAL_STALL_BEGIN\n", 1)[1].split("ORIGINAL_STALL_END\n", 1)[0]
    assert "Timeout (" in stalled and "stall_original_phase" in stalled
    assert "SDK pytest.main returned exit code 0" in original.stdout
    assert "ResourceWarning" not in original.stderr and "Exception ignored" not in original.stderr


@pytest.mark.parametrize("interval", ["0", "-1", "nan", "inf", "not-a-number"])
def test_invalid_diagnostic_interval_refuses_before_pytest(tmp_path: Path, interval: str) -> None:
    original = _run_diagnostic_child(
        tmp_path, 'raise AssertionError("Invalid diagnostic interval dispatched pytest")',
        "--diagnostic-interval", interval,
    )
    assert original.returncode == 2
    assert "diagnostic interval must be finite and positive" in original.stderr
    assert "SDK pytest.main returned" not in original.stdout


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
