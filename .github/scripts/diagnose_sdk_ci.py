"""Isolated hosted diagnosis; forced termination never counts as a native pass."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path.cwd()
out = Path(os.environ['RUNNER_TEMP']) / 'sdk-ci-native-diagnostic'
out.mkdir(exist_ok=False)
state = {
    'python': sys.version, 'executable': sys.executable,
    'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
    'source_candidate': 'a24c7b7fa97f37c6011cc771485f967714802a65',
    'diagnostic_only': True, 'all14_source_ci_success': False, 'probes': [],
}
changed = subprocess.check_output(['git', 'diff', '--name-only', state['source_candidate'], 'HEAD'], text=True).splitlines()
assert set(changed) == {'.github/workflows/ci.yml', '.github/scripts/diagnose_sdk_ci.py'}, changed
state['changed_paths'] = changed
state['wrapper_sha256'] = hashlib.sha256((root/'.github/scripts/run_sdk_tests.py').read_bytes()).hexdigest()


def save():
    (out/'state.json').write_text(json.dumps(state, indent=2))


def probe(name, command, timeout):
    record = {'name': name, 'command': command, 'deadline_seconds': timeout,
              'started_at': time.time(), 'forced_termination': False,
              'native_exit_verified': False}
    state['probes'].append(record)
    save()
    print('SDK_DIAGNOSTIC_START', name, flush=True)
    with (out/(name+'.log')).open('wb') as output:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT)
        record['owned_child_pid'] = child.pid
        save()
        deadline = time.monotonic() + timeout
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(5)
            print('SDK_DIAGNOSTIC_PROGRESS', name,
                  (out/(name+'.log')).stat().st_size, flush=True)
        if child.poll() is None:
            record['forced_termination'] = True
            # Our exact child only; a forced exit is diagnostic failure.
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=15)
        else:
            record['native_exit_verified'] = True
        record.update(exit_code=child.returncode, ended_at=time.time(),
                      raw_log_bytes=(out/(name+'.log')).stat().st_size)
        save()
    print('SDK_DIAGNOSTIC_END', name, json.dumps(record), flush=True)
    return record


save()
uv = probe('uv-entry', ['uv', 'run', '--no-sync', '--python',
           os.environ['MATRIX_PYTHON_VERSION'], '--group', 'test',
           'python', '-u', '-B', '-c',
           'import sys; print("SDK_UV_NATIVE_ENTRY", sys.executable, flush=True)'], 30)
native = probe('direct-native-full-suite', [sys.executable, '-u', '-B',
               '.github/scripts/run_sdk_tests.py', '-vv', '--timeout=120',
               '--timeout-method=thread'], 210)
state['diagnostic_complete'] = True
state['native_full_suite_passed'] = native['native_exit_verified'] and native['exit_code'] == 0
state['source_ci_gate_satisfied'] = False
save()
raise SystemExit(0 if state['native_full_suite_passed'] and
                 uv['native_exit_verified'] and uv['exit_code'] == 0 else 1)
