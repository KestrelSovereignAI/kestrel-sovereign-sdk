"""Keep native stack diagnostics alive through pytest and interpreter shutdown."""
from __future__ import annotations

import faulthandler
import sys


# Pytest's per-test faulthandler timer is cancelled when each test/session ends.
# Own this process timer until interpreter shutdown, including non-daemon joins.
# Disable only pytest's faulthandler plugin so it cannot cancel the same timer.
faulthandler.enable(all_threads=True)
faulthandler.dump_traceback_later(60, repeat=True)

import pytest  # noqa: E402

status = pytest.main(["-p", "no:faulthandler", *sys.argv[1:]])
print(f"SDK pytest.main returned exit code {status}; interpreter shutdown follows", flush=True)
raise SystemExit(status)
