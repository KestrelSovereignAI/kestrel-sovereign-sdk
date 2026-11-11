"""Keep native stack diagnostics alive through pytest and interpreter shutdown."""
from __future__ import annotations

import faulthandler
import os
import sys


# Spawned multiprocessing children re-run this script as ``__mp_main__``;
# only the parent may start diagnostics and the suite.
if __name__ == "__main__":
    # Keep diagnostics outside pytest capture for the original process lifetime.
    # The kernel retires this non-inheritable FD at native exit: no TextIO
    # finalizer or early atexit close can retire the diagnostic sink instead.
    _stderr = os.dup(2)
    # Pytest's exception/debugger hooks cancel the shared faulthandler timer.
    # Disable only that plugin; own this timer through interpreter thread joins.
    faulthandler.enable(file=_stderr, all_threads=True)
    faulthandler.dump_traceback_later(60, repeat=True, file=_stderr)

    import pytest  # noqa: E402

    status = pytest.main(["-p", "no:faulthandler", *sys.argv[1:]])
    print(f"SDK pytest.main returned exit code {status}; interpreter shutdown follows", flush=True)
    raise SystemExit(status)
