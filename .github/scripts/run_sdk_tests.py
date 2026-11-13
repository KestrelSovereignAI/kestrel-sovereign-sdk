"""Keep native stack diagnostics alive through pytest and interpreter shutdown."""
from __future__ import annotations

import argparse
import faulthandler
import math
import os
import sys


def _positive_interval(value: str) -> float:
    try:
        interval = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("diagnostic interval must be finite and positive") from error
    if not math.isfinite(interval) or interval <= 0:
        raise argparse.ArgumentTypeError("diagnostic interval must be finite and positive")
    return interval


class _ProgressDiagnostics:
    """Diagnose missing progress, not total wall time in a healthy suite."""

    def __init__(self, descriptor: int, interval: float) -> None:
        self._descriptor = descriptor
        self._interval = interval

    def progress(self) -> None:
        # The native lock-free walker is best-effort even on GIL builds
        # (CPython #140815/#158200). Invoke it only after missing progress,
        # never periodically while healthy tests churn frames/code metadata.
        faulthandler.dump_traceback_later(
            self._interval, repeat=True, file=self._descriptor,
        )

    def pytest_sessionstart(self) -> None:
        self.progress()

    def pytest_collection_finish(self) -> None:
        self.progress()

    def pytest_runtest_logreport(self) -> None:
        # Setup, call and teardown each count only when actually completed.
        self.progress()

    def pytest_sessionfinish(self) -> None:
        self.progress()


# Spawned multiprocessing children re-run this script as ``__mp_main__``;
# only the parent may start diagnostics and the suite.
if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--diagnostic-interval", type=_positive_interval, default=60.0)
    options, pytest_arguments = parser.parse_known_args()
    # Keep diagnostics outside pytest capture for the original process lifetime.
    # The kernel retires this non-inheritable FD at native exit: no TextIO
    # finalizer or early atexit close can retire the diagnostic sink instead.
    _stderr = os.dup(2)
    # Pytest's exception/debugger hooks cancel the shared faulthandler timer.
    # Disable only that plugin; own the inactivity timer through native exit.
    faulthandler.enable(file=_stderr, all_threads=True)
    _diagnostics = _ProgressDiagnostics(_stderr, options.diagnostic_interval)
    _diagnostics.progress()

    import pytest  # noqa: E402

    status = pytest.main(["-p", "no:faulthandler", *pytest_arguments], plugins=[_diagnostics])
    _diagnostics.progress()
    print(f"SDK pytest.main returned exit code {status}; interpreter shutdown follows", flush=True)
    raise SystemExit(status)
