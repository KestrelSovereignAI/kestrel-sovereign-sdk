"""Keep native stack diagnostics alive through pytest and interpreter shutdown."""
from __future__ import annotations

import faulthandler
import os
import sys


# Keep diagnostics outside pytest's file-descriptor capture, including crashes.
_stderr = os.fdopen(os.dup(2), "w")
# Pytest's exception/debugger hooks cancel the shared faulthandler timer.
# Disable only that plugin; own this timer through interpreter thread joins.
faulthandler.enable(file=_stderr, all_threads=True)
faulthandler.dump_traceback_later(60, repeat=True, file=_stderr)

import pytest  # noqa: E402

status = pytest.main(["-p", "no:faulthandler", *sys.argv[1:]])
print(f"SDK pytest.main returned exit code {status}; interpreter shutdown follows", flush=True)
raise SystemExit(status)
