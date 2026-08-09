"""Bounded, cancellation-safe subprocess retirement regressions (issue #66)."""

from __future__ import annotations

import asyncio
import gc
import logging
import re
import signal
import subprocess
import sys
import weakref
from pathlib import Path

import pytest

import kestrel_sdk.isolated_feature.client as client_module
from kestrel_sdk.isolated_feature import (
    FEATURE_EVENT,
    HEALTH,
    PROTOCOL_VERSION,
    JsonRpcNotification,
    encode_message,
)
from kestrel_sdk.isolated_feature.client import SubprocessIsolatedFeatureClient


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one local Git command for release-provenance semantic tests."""

    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


def _resolve_local_release_tag(checkout: Path, tag: str) -> str:
    """Model the resolver's exact-ref fetch and annotated-tag peel."""

    ref = f"refs/tags/{tag}"
    _git(checkout, "fetch", "--force", "--no-tags", "origin", f"+{ref}:{ref}")
    return _git(checkout, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def _final_revalidate_local_release_tag(
    checkout: Path, tag: str, expected_sha: str
) -> bool:
    """Model the final workflow gate against a possibly stale local tag."""

    ref = f"refs/tags/{tag}"
    remote_ref = _git(
        checkout,
        "ls-remote",
        "--exit-code",
        "--refs",
        "origin",
        ref,
        check=False,
    )
    if remote_ref.returncode != 0:
        return False
    fetched = _git(
        checkout,
        "fetch",
        "--force",
        "--no-tags",
        "origin",
        f"+{ref}:{ref}",
        check=False,
    )
    if fetched.returncode != 0:
        return False
    current_sha = _git(
        checkout, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False
    )
    return current_sha.returncode == 0 and current_sha.stdout.strip() == expected_sha


def _create_local_release_remote(tmp_path: Path) -> tuple[Path, Path, str]:
    """Create one bare remote and a separate checkout with an annotated tag."""

    remote = tmp_path / "origin.git"
    source = tmp_path / "source"
    verifier = tmp_path / "verifier"
    tag = "v0.33.1"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", str(source))
    _git(source, "config", "user.email", "tests@example.invalid")
    _git(source, "config", "user.name", "Release Test")
    _git(source, "commit", "--allow-empty", "-m", "release source")
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "tag", "-a", tag, "-m", "release")
    _git(source, "push", "origin", "HEAD:main", "refs/tags/v0.33.1")
    _git(tmp_path, "clone", str(remote), str(verifier))
    return source, verifier, tag


class _NeverReapedProcess:
    """Process double whose wait never settles until the test declares it dead."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.wait_started = asyncio.Event()
        self._reaped = asyncio.Event()
        self.wait_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0

    async def wait(self) -> int | None:
        self.wait_calls += 1
        self.wait_started.set()
        await self._reaped.wait()
        return self.returncode

    def finish(self, returncode: int = 42) -> None:
        self.returncode = returncode
        self._reaped.set()

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class _ExitedProcess:
    """Portable process double for retirement paths after child exit."""

    def __init__(self, returncode: int = 42) -> None:
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        raise AssertionError("an exited process must not be terminated")

    def kill(self) -> None:
        raise AssertionError("an exited process must not be killed")


class _GracefulProcess:
    """Live process double that settles its one wait after termination."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self._reaped = asyncio.Event()
        self.terminate_calls = 0
        self.kill_calls = 0

    async def wait(self) -> int:
        await self._reaped.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = 0
        self._reaped.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9
        self._reaped.set()


class _LateSpawnProcess:
    """Process double returned only after a cancelled spawn suppresses cancel."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdin = _CloseTrackingWriter()
        self.stdout = _TransportOnlyReader()
        self._reaped = asyncio.Event()
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    async def wait(self) -> int:
        self.wait_calls += 1
        await self._reaped.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = 0
        self._reaped.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9
        self._reaped.set()


class _LegacyCloseClient:
    """Compatibility double whose normal close completion returns None."""

    def __init__(self) -> None:
        self.close_calls = 0

    async def shutdown(self) -> dict[str, object]:
        return {}

    async def close(self) -> None:
        self.close_calls += 1


class _CancellationSuppressingClient:
    """Client double whose graceful phases ignore cancellation until released."""

    def __init__(self) -> None:
        self.shutdown_started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.release = asyncio.Event()
        self.shutdown_calls = 0
        self.close_calls = 0
        self.shutdown_cancellations = 0
        self.close_cancellations = 0

    async def shutdown(self) -> dict[str, object]:
        self.shutdown_calls += 1
        self.shutdown_started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.shutdown_cancellations += 1
        return {}

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.close_cancellations += 1


class _SecretPayload(dict):
    """Weak-referenceable child data used to prove retirement releases it."""


class _PayloadReturningShutdownClient:
    """A retired client whose shutdown result or error argument is sensitive."""

    def __init__(self, payload: _SecretPayload, *, fail_shutdown: bool) -> None:
        self._payload: _SecretPayload | None = payload
        self.fail_shutdown = fail_shutdown
        self.shutdown_calls = 0
        self.close_calls = 0

    async def shutdown(self) -> _SecretPayload:
        self.shutdown_calls += 1
        payload = self._payload
        self._payload = None
        assert payload is not None
        if self.fail_shutdown:
            raise RuntimeError(payload)
        return payload

    async def close(self) -> None:
        self.close_calls += 1


class _HostilePayloadReturningCloseClient:
    """A close implementation that delays then exposes child-controlled data."""

    def __init__(self, payload: _SecretPayload, *, fail_close: bool) -> None:
        self._payload: _SecretPayload | None = payload
        self.fail_close = fail_close
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.shutdown_calls = 0
        self.close_calls = 0

    async def shutdown(self) -> dict[str, object]:
        self.shutdown_calls += 1
        return {}

    async def close(self) -> _SecretPayload:
        self.close_calls += 1
        self.close_started.set()
        await self.release_close.wait()
        payload = self._payload
        self._payload = None
        assert payload is not None
        if self.fail_close:
            raise RuntimeError(payload)
        return payload


class _OrderedShutdownClient:
    """Client double that proves close waits for the graceful attempt."""

    def __init__(self) -> None:
        self.shutdown_started = asyncio.Event()
        self.release_shutdown = asyncio.Event()
        self.close_started = asyncio.Event()

    async def shutdown(self) -> dict[str, object]:
        self.shutdown_started.set()
        await self.release_shutdown.wait()
        return {}

    async def close(self) -> None:
        self.close_started.set()


class _CloseTrackingTransport:
    """Minimal reader transport double that records explicit release."""

    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _OrderedCloseTrackingTransport(_CloseTrackingTransport):
    """Transport double that records the Proactor-sensitive release order."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def close(self) -> None:
        self._events.append("stdout-close")
        super().close()


class _TransportOnlyReader:
    """Reader double that deliberately offers only asyncio's transport hook."""

    def __init__(self) -> None:
        self._transport = _CloseTrackingTransport()


class _CloseTrackingWriter:
    """Writer double whose close is immediate and portable."""

    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    async def wait_closed(self) -> None:
        return None


class _IndependentlyCancelledCloseWriter(_CloseTrackingWriter):
    """Writer whose close awaits a Future cancelled outside the client owner."""

    def __init__(self) -> None:
        super().__init__()
        self._waiter = asyncio.get_running_loop().create_future()
        self._waiter.cancel("writer-close-future-cancelled")

    async def wait_closed(self) -> None:
        await self._waiter


class _NonSettlingCloseWriter(_CloseTrackingWriter):
    """Writer double whose close remains pending until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.wait_started = asyncio.Event()
        self.release = asyncio.Event()

    async def wait_closed(self) -> None:
        self.wait_started.set()
        await self.release.wait()


class _SilentRequestWriter(_CloseTrackingWriter):
    """Writer double that accepts RPCs but deliberately never replies."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None


class _HostileCauseException(Exception):
    """Feature exception whose ordinary cause lookup is deliberately hostile."""

    def __getattribute__(self, name: str):
        if name == "__cause__":
            raise RuntimeError("hostile cause lookup")
        return super().__getattribute__(name)


class _HostileExceptionsPropertyGroup(ExceptionGroup):
    """ExceptionGroup subclass that rejects ordinary nested-member lookup."""

    @property
    def exceptions(self):
        raise RuntimeError("hostile ExceptionGroup exceptions lookup")


class _PendingCompletionError(RuntimeError):
    """Externally retained pending-Future hook failure for terminal-drain tests."""


class _FailingPendingCompletion:
    """Future-compatible double that rejects one terminal completion hook."""

    def __init__(self, failure_point: str) -> None:
        self.failure_point = failure_point
        self.error: _PendingCompletionError | None = None

    def _raise(self) -> None:
        # This local models decoded terminal data held through the read-loop
        # traceback until the client sanitizes the completion-hook failure.
        terminal_token = "pending-completion-terminal-secret"
        if not terminal_token:  # pragma: no cover - keeps the local live for traceback
            raise AssertionError("terminal token unexpectedly empty")
        self.error = _PendingCompletionError("pending completion hook failed")
        raise self.error

    def done(self) -> bool:
        if self.failure_point == "done":
            self._raise()
        return False

    def set_exception(self, exc: BaseException) -> None:
        if self.failure_point == "set_exception":
            self._raise()


class _NonCancellationFeatureExit(BaseException):
    """A bare feature failure that must not escape the reader task."""


class _UncancelingHealthClient:
    """Health suppresses the one stop cancellation until the test releases it."""

    def __init__(self) -> None:
        self.health_started = asyncio.Event()
        self.release_health = asyncio.Event()
        self.health_cancellations = 0

    async def health(self) -> dict[str, object]:
        self.health_started.set()
        while not self.release_health.is_set():
            try:
                await self.release_health.wait()
            except asyncio.CancelledError:
                self.health_cancellations += 1
                current = asyncio.current_task()
                assert current is not None
                current.uncancel()
        return {"ready": True}

    async def shutdown(self) -> dict[str, object]:
        return {}

    async def close(self) -> None:
        return None


class _QueuedReader:
    """Tiny line reader whose close produces EOF for live-handler tests."""

    def __init__(self) -> None:
        self._lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self._lines.get()

    def feed(self, line: bytes) -> None:
        self._lines.put_nowait(line)

    def close(self) -> None:
        self._lines.put_nowait(b"")


class _TransitionClient:
    """Controlled transition peer that fences itself on caller cancellation."""

    def __init__(self) -> None:
        self.replacement_required = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def prepare_config_transition(self, next_config):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.replacement_required = True
            raise
        return object()


async def _kill_and_reap_paused_subprocess(
    process: asyncio.subprocess.Process,
    retained: client_module.IsolatedFeatureClient,
) -> None:
    """Best-effort failed-test cleanup that cannot block on paused stdout."""

    if process.returncode is not None:
        return
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass
    # ``Process.wait()`` can remain blocked after kill while an asyncio
    # StreamReader has paused its pipe transport. Release and clear that stream
    # first, then bound reaping so teardown never replaces the test failure.
    retained._close_reader_transport()
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "failed to reap a killed paused-output subprocess", exc_info=exc
        )


@pytest.mark.asyncio
async def test_cancelled_stop_retains_detached_child_and_delivers_first_cancellation(
    monkeypatch,
):
    """Cancellation after public detachment cannot forget the exact process."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    process = _NeverReapedProcess()
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"], process=process)
    stop_task = asyncio.create_task(wrapper.stop())

    await asyncio.wait_for(process.wait_started.wait(), timeout=1)
    stop_task.cancel("first-stop-cancellation")

    with pytest.raises(asyncio.CancelledError) as raised:
        await stop_task

    # Cancellation is delivered only after bounded TERM/KILL/reap has completed.
    assert raised.value.args == ("first-stop-cancellation",)
    assert wrapper.client is None
    assert wrapper.process is None
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 1
    assert [retirement.process for retirement in wrapper._retirements] == [process]

    # A second stop must retry the authoritative record and cannot falsely
    # report success while the final reap remains non-settling.
    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 1
    assert [retirement.process for retirement in wrapper._retirements] == [process]

    # Once the same exact process is known reaped, a later retry succeeds and
    # releases the private handle.
    process.finish()
    await wrapper.stop()
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_stop_never_reaped_process_uses_only_signal_phase_observations(
    monkeypatch,
):
    """A process-only retry observes once after both signal intents are recorded."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.01)
    process = _NeverReapedProcess()
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"], process=process)
    observations = 0
    original_wait_for_process = wrapper._wait_for_process

    async def count_wait_observation(retirement):
        nonlocal observations
        observations += 1
        return await original_wait_for_process(retirement)

    monkeypatch.setattr(wrapper, "_wait_for_process", count_wait_observation)

    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()

    # Initial reap, post-TERM reap, and post-KILL reap are the complete
    # bounded sequence. Process.wait() itself remains a single retained task.
    assert observations == 3
    assert process.wait_calls == 1
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode is None
    assert len(wrapper._retirements) == 1
    assert wrapper._retirements[0].terminate_requested
    assert wrapper._retirements[0].kill_requested

    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()

    # The retry makes one fresh observation. Both signal intents are already
    # recorded, so it sends neither signal and does not repeat either
    # post-signal reap timeout.
    assert observations == 4
    assert process.wait_calls == 1
    assert process.terminate_calls == 1
    assert process.kill_calls == 1

    process.finish()
    await wrapper.stop()
    # Final settlement needs only the next initial observation.
    assert observations == 5
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_stop_does_not_reobserve_process_after_prior_close_task(monkeypatch):
    """A completed close from an earlier stop earns no new reap window."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.01)
    process = _NeverReapedProcess()

    class ShutdownCompletesLater:
        def __init__(self) -> None:
            self.release_shutdown = asyncio.Event()
            self.close_calls = 0

        async def shutdown(self) -> dict[str, object]:
            await self.release_shutdown.wait()
            return {}

        async def close(self) -> None:
            self.close_calls += 1

    client = ShutdownCompletesLater()
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"],
        client=client,  # type: ignore[arg-type]
        process=process,
    )
    observations = 0
    original_wait_for_process = wrapper._wait_for_process

    async def count_wait_observation(retirement):
        nonlocal observations
        observations += 1
        return await original_wait_for_process(retirement)

    monkeypatch.setattr(wrapper, "_wait_for_process", count_wait_observation)

    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()

    # Initial reap plus one observation after each signal. The client close is
    # already complete, but its graceful shutdown remains pending.
    assert observations == 3
    assert client.close_calls == 1

    client.release_shutdown.set()
    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()

    # The retry observes the process once at entry. It must not gain another
    # timeout merely for observing the close task completed above.
    assert observations == 4
    assert client.close_calls == 1

    process.finish()
    await wrapper.stop()
    assert observations == 5
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_stop_reobserves_process_after_client_close_can_unblock_waiter(
    monkeypatch,
):
    """A client close earns the one final reap observation it can unblock."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.01)
    process = _NeverReapedProcess()

    class CloseUnblocksWaiter:
        def __init__(self) -> None:
            self.close_calls = 0

        async def shutdown(self) -> dict[str, object]:
            return {}

        async def close(self) -> None:
            self.close_calls += 1
            process.finish()

    client = CloseUnblocksWaiter()
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"],
        client=client,  # type: ignore[arg-type]
        process=process,
    )
    observations = 0
    original_wait_for_process = wrapper._wait_for_process

    async def count_wait_observation(retirement):
        nonlocal observations
        observations += 1
        return await original_wait_for_process(retirement)

    monkeypatch.setattr(wrapper, "_wait_for_process", count_wait_observation)

    await wrapper.stop()

    assert observations == 4
    assert client.close_calls == 1
    assert process.wait_calls == 1
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_stop_timeout_does_not_fence_start_that_settles_before_timeout_update(
    monkeypatch,
):
    """A real timeout cannot overwrite a startup finally that queued first."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    spawn_started = asyncio.Event()
    spawn_cancelled = asyncio.Event()
    release_start_finally = asyncio.Event()

    async def stalled_spawn(*args, **kwargs):
        spawn_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            spawn_cancelled.set()
            # Hold cancellation just before _start() enters its finally, so the
            # test can put that finally ahead of stop's timeout update on the
            # state lock queue.
            await release_start_finally.wait()
            raise

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", stalled_spawn)
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"])
    start_task = asyncio.create_task(wrapper.start())
    await asyncio.wait_for(spawn_started.wait(), timeout=1)

    stop_task = asyncio.create_task(wrapper.stop())
    await asyncio.wait_for(spawn_cancelled.wait(), timeout=1)

    # The start finally queues before the real timeout can queue stop's state
    # update. asyncio.Lock's FIFO wakeup makes this controlled ordering
    # deterministic without faking _wait_for_start_settlement().
    await wrapper._state_lock.acquire()
    try:
        release_start_finally.set()
        await asyncio.sleep(0)
        await asyncio.sleep(client_module._SUBPROCESS_STOP_TIMEOUT * 2)
    finally:
        wrapper._state_lock.release()

    await stop_task
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert wrapper._starting is False
    assert wrapper._starting_generation is None
    assert wrapper._start_settled.is_set()
    assert wrapper._start_uncertain_generation is None
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_stop_preserves_nested_timeout_cancellation_counts(monkeypatch):
    """Nested timeout managers retain their own cancellation conversion state."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    process = _NeverReapedProcess()
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"], process=process)

    async def stop_inside_nested_timeouts() -> None:
        async with asyncio.timeout(0.03):
            async with asyncio.timeout(0.01):
                await wrapper.stop()

    stop_task = asyncio.create_task(stop_inside_nested_timeouts())
    try:
        await asyncio.wait_for(process.wait_started.wait(), timeout=1)
        with pytest.raises(TimeoutError):
            await stop_task
        assert stop_task.cancelling() == 0
    finally:
        process.finish()
        if not stop_task.done():
            stop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stop_task
        await wrapper.stop()


@pytest.mark.asyncio
async def test_stop_nested_timeout_and_external_cancels_continue_after_catch(
    monkeypatch,
):
    """Nested timeout bookkeeping survives newer external cancellation delivery."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    process = _NeverReapedProcess()
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"], process=process)

    async def stop_then_continue() -> tuple[tuple[object, ...], int]:
        current = asyncio.current_task()
        assert current is not None
        try:
            async with asyncio.timeout(1):
                async with asyncio.timeout(0.01):
                    await wrapper.stop()
        except asyncio.CancelledError as cancelled:
            caught = cancelled.args
        await asyncio.sleep(0)
        return caught, current.cancelling()

    task = asyncio.create_task(stop_then_continue())
    await asyncio.wait_for(process.wait_started.wait(), timeout=1)

    async with asyncio.timeout(1):
        while task.cancelling() < 1:
            await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel("external-cancel-one")
    async with asyncio.timeout(1):
        while task.cancelling() < 2:
            await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel("external-cancel-two")
    process.finish()

    assert await asyncio.wait_for(task, timeout=1) == (
        ("external-cancel-two",),
        2,
    )


@pytest.mark.asyncio
async def test_start_rejects_while_a_detached_child_has_unresolved_retirement(
    monkeypatch,
):
    """No replacement is spawned while an exact prior child is still uncertain."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    process = _NeverReapedProcess()
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"], process=process)

    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()

    spawned = False

    async def unexpected_spawn(*args, **kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("start must not spawn during unresolved retirement")

    monkeypatch.setattr(
        client_module.asyncio, "create_subprocess_exec", unexpected_spawn
    )
    with pytest.raises(RuntimeError, match="retirement is in progress"):
        await wrapper.start()
    assert spawned is False

    process.finish()
    await wrapper.stop()


@pytest.mark.asyncio
async def test_concurrent_start_is_rejected_while_stop_owns_retirement(monkeypatch):
    """A start racing a stop cannot spawn beside the supervisor's exact child."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.05)
    process = _NeverReapedProcess()
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"], process=process)
    stop_task = asyncio.create_task(wrapper.stop())

    try:
        await asyncio.wait_for(process.wait_started.wait(), timeout=1)
        with pytest.raises(RuntimeError, match="retirement is in progress"):
            await wrapper.start()
        with pytest.raises(RuntimeError, match="retirement is unresolved"):
            await stop_task
    finally:
        process.finish()
        await wrapper.stop()


@pytest.mark.asyncio
async def test_stop_cancellation_can_be_caught_then_awaited_without_replay(
    monkeypatch,
):
    """Manual delivery must not schedule a duplicate cancellation at next await."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    process = _NeverReapedProcess()
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"], process=process)

    async def stop_then_continue() -> tuple[tuple[object, ...], int]:
        current = asyncio.current_task()
        assert current is not None
        try:
            await wrapper.stop()
        except asyncio.CancelledError as cancelled:
            caught = cancelled.args
        await asyncio.sleep(0)
        return caught, current.cancelling()

    task = asyncio.create_task(stop_then_continue())
    await asyncio.wait_for(process.wait_started.wait(), timeout=1)
    task.cancel("external-cancel-one")
    await asyncio.sleep(0)
    task.cancel("external-cancel-two")
    process.finish()

    assert await asyncio.wait_for(task, timeout=1) == (
        ("external-cancel-two",),
        2,
    )


@pytest.mark.asyncio
async def test_repeated_cancel_before_startup_retirement_claim_settles_start(
    monkeypatch,
):
    """A second cancel cannot strand a child before its private claim runs."""

    spawned = asyncio.Event()
    release_spawn = asyncio.Event()
    process = _ExitedProcess()
    spawn_count = 0

    async def controlled_spawn(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count > 1:
            raise RuntimeError("replacement start admitted")
        spawned.set()
        await release_spawn.wait()
        process.stdin = _CloseTrackingWriter()
        process.stdout = _TransportOnlyReader()
        return process

    monkeypatch.setattr(
        client_module.asyncio, "create_subprocess_exec", controlled_spawn
    )
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"])
    start_task = asyncio.create_task(wrapper.start())
    await asyncio.wait_for(spawned.wait(), timeout=1)

    # The initial start has released this lock already. Hold it while its
    # cancellation path creates a private retirement task that must queue for
    # the claim, then cancel the public task a second time.
    await wrapper._state_lock.acquire()
    try:
        release_spawn.set()
        await asyncio.sleep(0)
        start_task.cancel("before-claim-one")
        await asyncio.sleep(0)
        start_task.cancel("before-claim-two")
        await asyncio.sleep(0)
    finally:
        wrapper._state_lock.release()

    with pytest.raises(asyncio.CancelledError) as raised:
        await asyncio.wait_for(start_task, timeout=1)
    assert raised.value.args == ("before-claim-two",)
    assert wrapper.client is None
    assert wrapper.process is None
    assert wrapper._retirements == []
    assert wrapper._starting is False
    assert wrapper._starting_generation is None
    assert wrapper._start_settled.is_set()

    # The old start slot is definitively settled, so this reaches spawning
    # rather than being rejected by a stale retirement/start fence.
    with pytest.raises(RuntimeError, match="replacement start admitted"):
        await wrapper.start()


@pytest.mark.asyncio
async def test_repeated_cancel_while_start_finalizer_waits_for_state_lock(
    monkeypatch,
):
    """A queued startup finalizer completes before the newer cancel escapes."""

    initialized = asyncio.Event()
    release_initialize = asyncio.Event()
    cleanup_finished = asyncio.Event()
    release_cleanup = asyncio.Event()
    process = _ExitedProcess()

    async def controlled_spawn(*args, **kwargs):
        process.stdin = _CloseTrackingWriter()
        process.stdout = _TransportOnlyReader()
        return process

    async def stalled_initialize(self, *args, **kwargs):
        initialized.set()
        await release_initialize.wait()
        return {}

    monkeypatch.setattr(
        client_module.asyncio, "create_subprocess_exec", controlled_spawn
    )
    monkeypatch.setattr(
        client_module.IsolatedFeatureClient,
        "initialize",
        stalled_initialize,
    )
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"])
    original_retire = wrapper._retire_startup_child

    async def hold_after_cleanup(client, child):
        result = await original_retire(client, child)
        cleanup_finished.set()
        await release_cleanup.wait()
        return result

    monkeypatch.setattr(wrapper, "_retire_startup_child", hold_after_cleanup)
    start_task = asyncio.create_task(wrapper.start())
    await asyncio.wait_for(initialized.wait(), timeout=1)
    start_task.cancel("finalizer-one")
    await asyncio.wait_for(cleanup_finished.wait(), timeout=1)

    await wrapper._state_lock.acquire()
    try:
        release_cleanup.set()
        await asyncio.sleep(0)
        start_task.cancel("finalizer-two")
        await asyncio.sleep(0)
    finally:
        wrapper._state_lock.release()

    with pytest.raises(asyncio.CancelledError) as raised:
        await asyncio.wait_for(start_task, timeout=1)
    assert raised.value.args == ("finalizer-two",)
    assert wrapper.client is None
    assert wrapper.process is None
    assert wrapper._retirements == []
    assert wrapper._starting is False
    assert wrapper._starting_generation is None
    assert wrapper._start_settled.is_set()


@pytest.mark.asyncio
async def test_start_finalizer_cancellation_retires_successfully_published_child(
    monkeypatch,
):
    """A cancellation before start() returns cannot leave its child published."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    process = _GracefulProcess()
    entered_finalizer = asyncio.Event()
    release_finalizer = asyncio.Event()

    async def spawn(*args, **kwargs):
        process.stdin = _CloseTrackingWriter()
        process.stdout = _TransportOnlyReader()
        return process

    async def initialize(self, *args, **kwargs):
        return {}

    async def health(self):
        return {"ready": True}

    async def list_tools(self):
        return []

    async def shutdown(self):
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "initialize", initialize)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "health", health)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "list_tools", list_tools)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "shutdown", shutdown)
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"])
    original_finalizer = wrapper._finalize_start

    async def delayed_finalizer(generation):
        await original_finalizer(generation)
        entered_finalizer.set()
        await release_finalizer.wait()

    monkeypatch.setattr(wrapper, "_finalize_start", delayed_finalizer)
    start_task = asyncio.create_task(wrapper.start())
    await asyncio.wait_for(entered_finalizer.wait(), timeout=1)

    start_task.cancel("cancel-during-success-finalizer")
    await asyncio.sleep(0)
    release_finalizer.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await asyncio.wait_for(start_task, timeout=1)
    assert raised.value.args == ("cancel-during-success-finalizer",)
    assert wrapper.client is None
    assert wrapper.process is None
    assert wrapper._retirements == []
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


@pytest.mark.asyncio
async def test_successful_start_does_not_await_outer_unregistration(
    monkeypatch,
):
    """A successful start cannot suspend in the former unregister handoff."""

    process = _GracefulProcess()

    async def spawn(*args, **kwargs):
        process.stdin = _CloseTrackingWriter()
        process.stdout = _TransportOnlyReader()
        return process

    async def initialize(self, *args, **kwargs):
        return {}

    async def health(self):
        return {"ready": True}

    async def list_tools(self):
        return []

    async def shutdown(self):
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "initialize", initialize)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "health", health)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "list_tools", list_tools)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "shutdown", shutdown)
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"])

    async def unexpected_unregistration(operation):
        raise AssertionError(
            "successful start must not await the old unregister handoff"
        )

    monkeypatch.setattr(wrapper, "_unregister_operation", unexpected_unregistration)
    await wrapper.start()
    assert wrapper.client is not None
    assert wrapper.process is process
    await wrapper.stop()
    assert wrapper._retirements == []
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


@pytest.mark.asyncio
async def test_stop_cannot_detach_a_child_in_start_success_unregistration_gap(
    monkeypatch,
):
    """A stop in the former unregister gap cannot make start return detached."""

    process = _GracefulProcess()
    entered_old_gap = asyncio.Event()
    release_old_gap = asyncio.Event()

    async def spawn(*args, **kwargs):
        process.stdin = _CloseTrackingWriter()
        process.stdout = _TransportOnlyReader()
        return process

    async def initialize(self, *args, **kwargs):
        return {}

    async def health(self):
        return {"ready": True}

    async def list_tools(self):
        return []

    async def shutdown(self):
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "initialize", initialize)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "health", health)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "list_tools", list_tools)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "shutdown", shutdown)
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"])
    original_unregister = wrapper._unregister_operation

    async def delay_old_gap(operation):
        await original_unregister(operation)
        entered_old_gap.set()
        await release_old_gap.wait()

    # The vulnerable implementation invokes this successful-start cleanup and
    # yields after it has removed the outer operation. The fixed implementation
    # removes it synchronously at the return boundary, so this exact old gap is
    # never entered on the success path.
    monkeypatch.setattr(wrapper, "_unregister_operation", delay_old_gap)
    observed_at_start_return: list[
        tuple[client_module.IsolatedFeatureClient | None, _GracefulProcess | None]
    ] = []

    async def direct_parent() -> None:
        await wrapper.start()
        observed_at_start_return.append((wrapper.client, wrapper.process))

    start_task = asyncio.create_task(direct_parent())
    gap_waiter = asyncio.create_task(entered_old_gap.wait())
    done, _ = await asyncio.wait(
        {start_task, gap_waiter}, return_when=asyncio.FIRST_COMPLETED
    )

    if gap_waiter in done:
        # No explicit cancellation: this stop occupies the old post-unregister
        # wake-up gap and detaches the newly published child before start can
        # resume. The old implementation would then return success with None
        # handles, which the done callback records below.
        stop_task = asyncio.create_task(wrapper.stop())
        async with asyncio.timeout(1):
            while wrapper.client is not None or wrapper.process is not None:
                await asyncio.sleep(0)
        release_old_gap.set()
        await asyncio.wait_for(start_task, timeout=1)
        await asyncio.wait_for(stop_task, timeout=1)
    else:
        assert start_task in done
        gap_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gap_waiter
        await start_task
        await wrapper.stop()

    assert observed_at_start_return
    returned_client, returned_process = observed_at_start_return[0]
    assert returned_client is not None
    assert returned_process is process


@pytest.mark.asyncio
async def test_cross_task_stop_does_not_cancel_long_lived_direct_start_parent(
    monkeypatch,
):
    """A completed direct start must not leave its parent registered to stop()."""

    process = _GracefulProcess()
    started_after_return = asyncio.Event()
    release_parent = asyncio.Event()
    parent_cancelled = asyncio.Event()

    async def spawn(*args, **kwargs):
        process.stdin = _CloseTrackingWriter()
        process.stdout = _TransportOnlyReader()
        return process

    async def initialize(self, *args, **kwargs):
        return {}

    async def health(self):
        return {"ready": True}

    async def list_tools(self):
        return []

    async def shutdown(self):
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "initialize", initialize)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "health", health)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "list_tools", list_tools)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "shutdown", shutdown)
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"])

    async def long_lived_parent() -> None:
        try:
            await wrapper.start()
            assert asyncio.current_task() not in wrapper._active_operations
            started_after_return.set()
            await release_parent.wait()
        except asyncio.CancelledError:
            parent_cancelled.set()
            raise

    parent = asyncio.create_task(long_lived_parent())
    await asyncio.wait_for(started_after_return.wait(), timeout=1)

    # This test task is T2. It must retire the child, not transfer/cancel the
    # unrelated long-lived T1 that directly awaited start() above.
    await wrapper.stop()
    assert not parent.done()
    assert not parent_cancelled.is_set()
    assert wrapper._active_operations == set()

    release_parent.set()
    await asyncio.wait_for(parent, timeout=1)
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


@pytest.mark.asyncio
async def test_same_task_start_stop_restart_then_cross_task_stop_keeps_parent_alive(
    monkeypatch,
):
    """Same-task lifecycle calls cannot leave stale ownership for a later stop."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    first_process = _GracefulProcess()
    second_process = _GracefulProcess()
    processes = [first_process, second_process]
    restarted_after_return = asyncio.Event()
    release_parent = asyncio.Event()
    parent_cancelled = asyncio.Event()

    async def spawn(*args, **kwargs):
        process = processes.pop(0)
        process.stdin = _CloseTrackingWriter()
        process.stdout = _TransportOnlyReader()
        return process

    async def initialize(self, *args, **kwargs):
        return {}

    async def health(self):
        return {"ready": True}

    async def list_tools(self):
        return []

    async def shutdown(self):
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "initialize", initialize)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "health", health)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "list_tools", list_tools)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "shutdown", shutdown)
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"])

    async def long_lived_parent() -> None:
        try:
            await wrapper.start()
            await wrapper.stop()
            await wrapper.start()
            assert asyncio.current_task() not in wrapper._active_operations
            restarted_after_return.set()
            await release_parent.wait()
        except asyncio.CancelledError:
            parent_cancelled.set()
            raise

    parent = asyncio.create_task(long_lived_parent())
    try:
        await asyncio.wait_for(restarted_after_return.wait(), timeout=1)
    except TimeoutError:
        assert parent.done()
        await parent
    assert first_process.terminate_calls == 1
    assert first_process.kill_calls == 0

    # A different T2 performs the later stop after the same T1 restarted.
    await wrapper.stop()
    assert not parent.done()
    assert not parent_cancelled.is_set()
    assert second_process.terminate_calls == 1
    assert second_process.kill_calls == 0

    release_parent.set()
    await asyncio.wait_for(parent, timeout=1)


@pytest.mark.asyncio
async def test_successful_direct_start_releases_at_return_boundary_without_retention(
    monkeypatch,
):
    """A direct await releases successful-start ownership before later work."""

    process = _GracefulProcess()

    async def spawn(*args, **kwargs):
        process.stdin = _CloseTrackingWriter()
        process.stdout = _TransportOnlyReader()
        return process

    async def initialize(self, *args, **kwargs):
        return {}

    async def health(self):
        return {"ready": True}

    async def list_tools(self):
        return []

    async def shutdown(self):
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "initialize", initialize)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "health", health)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "list_tools", list_tools)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "shutdown", shutdown)
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"])

    try:
        for _ in range(2):
            await wrapper.start()
            assert asyncio.current_task() not in wrapper._active_operations
            assert wrapper._active_operations == set()
            assert wrapper._retiring_operations == set()
    finally:
        await wrapper.stop()


@pytest.mark.asyncio
async def test_stop_closes_stdout_transport_held_by_external_client_reference():
    """A successful retirement releases stdout even when the client is retained."""

    reader = _TransportOnlyReader()
    writer = _CloseTrackingWriter()
    retained = client_module.IsolatedFeatureClient(reader, writer)

    async def successful_shutdown() -> dict[str, object]:
        return {}

    retained.shutdown = successful_shutdown  # type: ignore[method-assign]
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"],
        client=retained,
        process=_ExitedProcess(),
    )

    await wrapper.stop()

    assert retained.reader is reader
    assert reader._transport.close_calls == 1
    assert writer.close_calls == 1
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_stop_signals_live_child_before_closing_retained_stdout_transport(
    monkeypatch,
):
    """Proactor-safe retirement does not invalidate a live stdout pipe handle."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    events: list[str] = []
    reader = _TransportOnlyReader()
    reader._transport = _OrderedCloseTrackingTransport(events)
    writer = _CloseTrackingWriter()
    retained = client_module.IsolatedFeatureClient(reader, writer)
    release_wait = asyncio.Event()

    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None

        async def wait(self) -> int:
            events.append("wait-started")
            await release_wait.wait()
            events.append("wait-finished")
            return self.returncode

        def terminate(self) -> None:
            events.append("terminate")
            # A real subprocess transport can publish this before its retained
            # waiter has completed its own cleanup.
            self.returncode = 0
            release_wait.set()

        def kill(self) -> None:
            raise AssertionError("terminate must allow the bounded reap")

    async def successful_shutdown() -> dict[str, object]:
        events.append("shutdown")
        return {}

    retained.shutdown = successful_shutdown  # type: ignore[method-assign]
    process = Process()
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"],
        client=retained,
        process=process,  # type: ignore[arg-type]
    )

    await wrapper.stop()

    assert events.index("terminate") < events.index("wait-finished")
    assert events.index("wait-finished") < events.index("stdout-close")
    assert reader._transport.close_calls == 1
    assert writer.close_calls == 1
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_stop_hard_bounds_cancellation_suppressing_client_phases(monkeypatch):
    """A deadline never waits for shutdown/close cancellation to finish."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    retained = _CancellationSuppressingClient()
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=retained, process=_ExitedProcess()
    )

    started_at = asyncio.get_running_loop().time()
    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()
    elapsed = asyncio.get_running_loop().time() - started_at

    # Two deadline observations are all this fake process needs. The old
    # wait_for() path never returned because both coroutines consumed cancel.
    assert elapsed < 0.15
    assert retained.shutdown_started.is_set()
    assert retained.close_started.is_set()
    assert retained.shutdown_calls == 1
    assert retained.close_calls == 1
    assert retained.shutdown_cancellations == 0
    assert retained.close_cancellations == 0
    retirement = wrapper._retirements[0]
    shutdown_task = retirement.shutdown_task
    close_task = retirement.close_task
    assert shutdown_task is not None and not shutdown_task.done()
    assert close_task is not None and not close_task.done()
    assert retirement.client is retained

    # A later retry observes the original owned tasks after they settle; it
    # does not issue duplicate graceful requests or mistake the first timeout
    # for success.
    retained.release.set()
    await wrapper.stop()
    assert shutdown_task.done()
    assert close_task.done()
    assert retained.shutdown_calls == 1
    assert retained.close_calls == 1
    assert wrapper._retirements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_shutdown", (False, True), ids=("result", "exception"))
async def test_unreaped_retirement_releases_shutdown_payload_before_stop_returns(
    monkeypatch,
    fail_shutdown,
):
    """A blocked process reap does not retain a completed shutdown payload."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.05)
    process = _NeverReapedProcess()
    payload = _SecretPayload(token="secret")
    payload_ref = weakref.ref(payload)
    retained = _PayloadReturningShutdownClient(payload, fail_shutdown=fail_shutdown)
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"],
        client=retained,  # type: ignore[arg-type]
        process=process,
    )

    stop_task = asyncio.create_task(wrapper.stop())
    await asyncio.wait_for(process.wait_started.wait(), timeout=1)
    retirement = wrapper._retirements[0]
    async with asyncio.timeout(1):
        while retirement.shutdown_task is not None:
            await asyncio.sleep(0)

    assert retirement.shutdown_attempted
    assert retirement.shutdown_settled
    assert retirement.shutdown_succeeded is not fail_shutdown
    assert retirement.shutdown_task is None
    assert retained.shutdown_calls == 1
    assert not stop_task.done()

    del payload
    gc.collect()
    assert payload_ref() is None

    process.finish()
    await stop_task
    assert retained.shutdown_calls == 1
    assert retained.close_calls == 1
    assert wrapper._retirements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_close", (False, True), ids=("result", "exception"))
async def test_unreaped_retirement_releases_hostile_close_payload_before_stop_returns(
    monkeypatch,
    fail_close,
):
    """The final bounded reap does not retain hostile close data."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.1)
    final_reap_started = asyncio.Event()
    primary_final_reap_started = asyncio.Event()
    sibling_observations = 0
    primary_observations = 0
    original_wait_for_process = SubprocessIsolatedFeatureClient._wait_for_process
    sibling: client_module._ChildRetirement | None = None

    async def observe_wait_for_process(self, current_retirement):
        nonlocal sibling_observations, primary_observations
        if current_retirement is sibling:
            sibling_observations += 1
            if sibling_observations == 4:
                final_reap_started.set()
        elif current_retirement.process is process:
            primary_observations += 1
            if primary_observations == 4:
                primary_final_reap_started.set()
        return await original_wait_for_process(self, current_retirement)

    monkeypatch.setattr(
        SubprocessIsolatedFeatureClient,
        "_wait_for_process",
        observe_wait_for_process,
    )
    process = _NeverReapedProcess()
    sibling_process = _NeverReapedProcess()
    payload = _SecretPayload(token="secret")
    payload_ref = weakref.ref(payload)
    retained = _HostilePayloadReturningCloseClient(payload, fail_close=fail_close)
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"],
        client=retained,  # type: ignore[arg-type]
        process=process,
    )
    async with wrapper._state_lock:
        sibling = wrapper._claim_retirement_locked(
            _LegacyCloseClient(),
            sibling_process,  # type: ignore[arg-type]
        )
    assert sibling is not None

    stop_task = asyncio.create_task(wrapper.stop())
    await asyncio.wait_for(retained.close_started.wait(), timeout=1)
    primary = next(item for item in wrapper._retirements if item.client is retained)
    retained.release_close.set()
    async with asyncio.timeout(1):
        while primary.close_task is not None:
            await asyncio.sleep(0)
    if fail_close:
        assert primary.task is not None
        assert not await asyncio.wait_for(asyncio.shield(primary.task), timeout=1)
    else:
        await asyncio.wait_for(primary_final_reap_started.wait(), timeout=1)
    await asyncio.wait_for(final_reap_started.wait(), timeout=1)

    assert primary.close_attempted
    assert primary.close_settled
    assert primary.close_succeeded is not fail_close
    assert primary.close_task is None
    assert retained.shutdown_calls == 1
    assert retained.close_calls == 1
    assert sibling_observations == 4
    assert not stop_task.done()

    del payload
    gc.collect()
    assert payload_ref() is None

    process.finish()
    sibling_process.finish()
    if fail_close:
        with pytest.raises(RuntimeError, match="retirement is unresolved"):
            await stop_task
        with pytest.raises(RuntimeError, match="retirement is in progress"):
            await wrapper.start()
    else:
        await stop_task
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert bool(wrapper._retirements) is fail_close


@pytest.mark.asyncio
async def test_stop_retires_uncanceling_operations_and_fences_replacement(monkeypatch):
    """An uncanceling health task cannot outlive a successful stop unnoticed."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    retained = _UncancelingHealthClient()
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=retained, process=_ExitedProcess()
    )
    health_task = asyncio.create_task(wrapper.health())
    await asyncio.wait_for(retained.health_started.wait(), timeout=1)

    # This start has registered but is queued behind the health task's old
    # lifecycle lock. stop() must transfer/cancel it synchronously too rather
    # than letting it start after health eventually releases the lock.
    queued_start = asyncio.create_task(wrapper.start())
    async with asyncio.timeout(1):
        while len(wrapper._active_operations) != 2:
            await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()

    assert retained.health_cancellations == 1
    assert health_task in wrapper._retiring_operations
    assert health_task in wrapper._operation_cancel_requested
    assert wrapper._active_operations == set()
    with pytest.raises(asyncio.CancelledError):
        await queued_start

    # A retry observes the exact retained task; uncancel() must not make stop
    # deliver a second cancellation. New lifecycle work fails before waiting on
    # the stale _lifecycle_lock owner.
    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()
    assert retained.health_cancellations == 1
    with pytest.raises(RuntimeError, match="retirement is in progress"):
        await wrapper.start()
    with pytest.raises(RuntimeError, match="retirement is in progress"):
        await wrapper.health()
    with pytest.raises(RuntimeError, match="retirement is in progress"):
        await wrapper.prepare_config_transition({"replacement": "blocked"})

    retained.release_health.set()
    with pytest.raises(RuntimeError, match="stopped during health check"):
        await health_task
    await wrapper.stop()
    assert wrapper._retiring_operations == set()
    assert wrapper._operation_cancel_requested == set()


@pytest.mark.asyncio
async def test_retired_operation_failure_is_observed_without_consuming_later_await(
    monkeypatch,
):
    """Transferred task failures are retrieved before release, not left to GC."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    retained = _UncancelingHealthClient()
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=retained, process=_ExitedProcess()
    )
    health_task = asyncio.create_task(wrapper.health())
    await asyncio.wait_for(retained.health_started.wait(), timeout=1)

    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()
    retained.release_health.set()
    async with asyncio.timeout(1):
        while not health_task.done():
            await asyncio.sleep(0)
    await asyncio.sleep(0)
    # The done callback retrieves the fence failure synchronously.  This
    # clears asyncio's unretrieved-exception flag without replacing the task's
    # result, so an owner that retained the Task can still await it below.
    assert health_task._log_traceback is False

    with pytest.raises(RuntimeError, match="stopped during health check"):
        await health_task
    await wrapper.stop()


@pytest.mark.asyncio
async def test_retired_operation_remains_fenced_until_its_outer_task_is_done(
    monkeypatch,
):
    """Child finalizer cleanup cannot admit a replacement one turn too early."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    retained = _UncancelingHealthClient()
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=retained, process=_ExitedProcess()
    )
    entered_outer_finally = asyncio.Event()
    release_outer_finally = asyncio.Event()
    original_unregister = wrapper._unregister_operation_owned
    health_task: asyncio.Task[dict[str, object]] | None = None

    async def hold_after_unregister(operation, cancellations):
        await original_unregister(operation, cancellations)
        if operation is health_task:
            entered_outer_finally.set()
            await release_outer_finally.wait()

    monkeypatch.setattr(wrapper, "_unregister_operation_owned", hold_after_unregister)
    health_task = asyncio.create_task(wrapper.health())
    await asyncio.wait_for(retained.health_started.wait(), timeout=1)
    stop_task = asyncio.create_task(wrapper.stop())
    async with asyncio.timeout(1):
        while retained.health_cancellations != 1:
            await asyncio.sleep(0)
    retained.release_health.set()
    await asyncio.wait_for(entered_outer_finally.wait(), timeout=1)

    assert health_task in wrapper._retiring_operations
    assert not health_task.done()
    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await stop_task

    # stop() has returned and _stopping is false, so only transferred-operation
    # ownership can block this replacement. The old task is still in its outer
    # finally and therefore must remain an admission fence.
    with pytest.raises(RuntimeError, match="retirement is in progress"):
        await wrapper.start()

    release_outer_finally.set()
    with pytest.raises(RuntimeError, match="stopped during health check"):
        await health_task
    async with asyncio.timeout(1):
        while wrapper._retiring_operations:
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stop_observes_graceful_shutdown_before_starting_close():
    """Close cannot preempt a graceful shutdown that is still making progress."""

    retained = _OrderedShutdownClient()
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=retained, process=_ExitedProcess()
    )
    stop_task = asyncio.create_task(wrapper.stop())
    await asyncio.wait_for(retained.shutdown_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not retained.close_started.is_set()

    retained.release_shutdown.set()
    await stop_task
    assert retained.close_started.is_set()
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_stop_reobserves_shutdown_settled_by_close(monkeypatch):
    """A timed-out graceful RPC that close settles succeeds in this stop call."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    reader = _QueuedReader()
    writer = _SilentRequestWriter()
    retained = client_module.IsolatedFeatureClient(reader, writer)
    await retained.start()
    assert retained._read_task is not None
    read_task = retained._read_task
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=retained, process=_ExitedProcess()
    )

    await wrapper.stop()

    assert writer.writes
    assert read_task.done()
    assert retained._close_task is not None and retained._close_task.done()
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_stop_keeps_buffered_drain_owner_until_stubborn_handler_settles(
    monkeypatch,
):
    """A buffered event drain fences restart until its handler is truly gone."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    reader = _TransportOnlyReader()
    writer = _CloseTrackingWriter()
    retained = client_module.IsolatedFeatureClient(reader, writer)
    retained._pending_notifications.append({"payload": {"secret": "buffered"}})
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stubborn_handler(params):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    async def successful_shutdown() -> dict[str, object]:
        return {}

    retained.shutdown = successful_shutdown  # type: ignore[method-assign]
    retained.on_event(stubborn_handler)
    await asyncio.wait_for(entered.wait(), timeout=1)
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=retained, process=_ExitedProcess()
    )

    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()
    assert wrapper._retirements[0].client is retained
    assert retained._close_task is not None and not retained._close_task.done()
    with pytest.raises(RuntimeError, match="retirement is in progress"):
        await wrapper.start()

    release.set()
    await wrapper.stop()
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_stop_keeps_live_read_handler_owner_until_stubborn_handler_settles(
    monkeypatch,
):
    """A cancellation-resistant live handler in the read task also fences restart."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    reader = _QueuedReader()
    writer = _CloseTrackingWriter()
    retained = client_module.IsolatedFeatureClient(reader, writer)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stubborn_handler(params):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    async def successful_shutdown() -> dict[str, object]:
        return {}

    retained.shutdown = successful_shutdown  # type: ignore[method-assign]
    retained.on_event(stubborn_handler)
    await retained.start()
    reader.feed(
        encode_message(
            JsonRpcNotification(method=FEATURE_EVENT, params={"payload": "live"})
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=retained, process=_ExitedProcess()
    )

    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()
    assert wrapper._retirements[0].client is retained
    assert retained._read_task is not None and not retained._read_task.done()

    release.set()
    await wrapper.stop()
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_successful_close_clears_buffered_child_event_payloads():
    """Successful coordinated disposal releases decoded secret-bearing events."""

    reader = _TransportOnlyReader()
    writer = _CloseTrackingWriter()
    retained = client_module.IsolatedFeatureClient(reader, writer)
    secret_event = {"payload": {"token": "not-retained-after-stop"}}
    retained._pending_notifications.append(secret_event)

    async def successful_shutdown() -> dict[str, object]:
        return {}

    retained.shutdown = successful_shutdown  # type: ignore[method-assign]
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=retained, process=_ExitedProcess()
    )

    await wrapper.stop()

    assert not retained._pending_notifications
    assert retained._event_handlers == []
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_stop_releases_event_data_while_writer_close_remains_pending(
    monkeypatch,
):
    """A wedged writer close retains the fence, not child event data."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    reader = _QueuedReader()
    writer = _NonSettlingCloseWriter()
    retained = client_module.IsolatedFeatureClient(reader, writer)
    await retained.start()

    class SecretPayload(dict):
        pass

    class Handler:
        def __call__(self, params):
            return None

    payload = SecretPayload(token="event-secret-not-retained-by-pending-close")
    handler = Handler()
    payload_ref = weakref.ref(payload)
    handler_ref = weakref.ref(handler)
    retained._pending_notifications.append({"payload": payload})
    # Seed retained state directly so registering the handler cannot schedule a
    # buffered drain that releases the payload before close begins.
    retained._event_handlers.append(handler)
    del payload
    del handler

    async def successful_shutdown() -> dict[str, object]:
        return {}

    retained.shutdown = successful_shutdown  # type: ignore[method-assign]
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=retained, process=_ExitedProcess()
    )

    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()
    await asyncio.wait_for(writer.wait_started.wait(), timeout=1)

    assert retained._close_task is not None and not retained._close_task.done()
    assert not retained._pending_notifications
    assert retained._event_handlers == []

    # The direct inner-client API can still be reached through an external
    # reference while the writer close is pending. It must not retain a handler
    # offered after close has started.
    late_handler = Handler()
    late_handler_ref = weakref.ref(late_handler)
    retained.on_event(late_handler)
    del late_handler
    gc.collect()
    assert payload_ref() is None
    assert handler_ref() is None
    assert late_handler_ref() is None
    assert retained._event_handlers == []

    # Wrapper registrations remain durable for the next generation even
    # though this retired inner client ignores its direct registration.
    future_handler = Handler()
    wrapper.on_event(future_handler)
    assert wrapper._handlers == [future_handler]
    with pytest.raises(RuntimeError, match="retirement is in progress"):
        await wrapper.start()

    writer.release.set()
    await wrapper.stop()
    assert wrapper._retirements == []

    replacement_process = _GracefulProcess()
    replacement_reader = _QueuedReader()
    replacement_writer = _CloseTrackingWriter()

    async def spawn(*args, **kwargs):
        replacement_process.stdin = replacement_writer
        replacement_process.stdout = replacement_reader
        return replacement_process

    async def initialize(self, *args, **kwargs):
        return {}

    async def health(self):
        return {"ready": True}

    async def list_tools(self):
        return []

    async def shutdown(self):
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "initialize", initialize)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "health", health)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "list_tools", list_tools)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "shutdown", shutdown)

    try:
        await wrapper.start()
        assert wrapper.client is not None
        assert wrapper.client._event_handlers == [future_handler]
    finally:
        await wrapper.stop()


@pytest.mark.asyncio
async def test_close_propagates_same_turn_read_completion_cancellation():
    """A read done callback cannot make close swallow its caller cancellation."""

    reader = _QueuedReader()
    writer = _CloseTrackingWriter()
    client = client_module.IsolatedFeatureClient(reader, writer)
    await client.start()
    assert client._read_task is not None
    close_task: asyncio.Task[None] | None = None

    def cancel_close_when_read_finishes(done: asyncio.Task[None]) -> None:
        assert close_task is not None
        close_task.cancel("read-task-done")

    client._read_task.add_done_callback(cancel_close_when_read_finishes)
    close_task = asyncio.create_task(client.close())

    with pytest.raises(asyncio.CancelledError) as raised:
        await close_task
    assert raised.value.args == ("read-task-done",)
    assert close_task.cancelling() == 1
    assert client._close_task is not None
    assert await client._close_task is None


@pytest.mark.asyncio
async def test_independently_cancelled_writer_close_does_not_fence_replacement(
    monkeypatch,
):
    """A cancelled writer Future is a close failure, not cancellation of its owner."""

    retained = client_module.IsolatedFeatureClient(
        _TransportOnlyReader(), _IndependentlyCancelledCloseWriter()
    )

    async def successful_shutdown() -> dict[str, object]:
        return {}

    retained.shutdown = successful_shutdown  # type: ignore[method-assign]
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=retained, process=_ExitedProcess()
    )

    await wrapper.stop()

    assert retained._close_task is not None
    assert retained._close_task.done()
    assert not retained._close_task.cancelled()
    assert retained._close_task.cancelling() == 0
    assert wrapper._retirements == []

    spawned = False

    async def replacement_spawn(*args, **kwargs):
        nonlocal spawned
        spawned = True
        raise RuntimeError("replacement start admitted")

    monkeypatch.setattr(
        client_module.asyncio, "create_subprocess_exec", replacement_spawn
    )
    with pytest.raises(RuntimeError, match="replacement start admitted"):
        await wrapper.start()
    assert spawned is True


@pytest.mark.asyncio
async def test_repeated_transition_cancellation_finishes_config_handoff_once():
    """A private finisher clears pending config before the newest cancel escapes."""

    old_config = {"credential": "old"}
    next_config = {"credential": "next"}
    client = _TransitionClient()
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=client, config=old_config
    )
    transition_task = asyncio.create_task(
        wrapper.prepare_config_transition(next_config)
    )
    await asyncio.wait_for(client.started.wait(), timeout=1)

    await wrapper._state_lock.acquire()
    try:
        transition_task.cancel("transition-one")
        await asyncio.sleep(0)
        transition_task.cancel("transition-two")
        await asyncio.sleep(0)
    finally:
        wrapper._state_lock.release()

    with pytest.raises(asyncio.CancelledError) as raised:
        await asyncio.wait_for(transition_task, timeout=1)
    assert raised.value.args == ("transition-two",)
    assert transition_task.cancelling() == 2
    assert wrapper.config is next_config
    assert wrapper._pending_transition is None
    assert wrapper._active_operations == set()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX SIGKILL")
async def test_real_subprocess_ignoring_sigterm_is_killed_and_reaped(monkeypatch):
    """A real SIGTERM-resistant child reaches the bounded SIGKILL reap path."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.1)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "while True: time.sleep(0.01)\n"
        ),
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert await asyncio.wait_for(process.stdout.readline(), timeout=1) == b"ready\n"
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"], process=process)

    try:
        await wrapper.stop()
        assert process.returncode == -signal.SIGKILL
        assert wrapper.client is None
        assert wrapper.process is None
        assert wrapper._retirements == []
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX pipe transport")
async def test_stop_closes_paused_stdout_pipe_held_by_retained_client(monkeypatch):
    """A real paused stdout pipe is released before successful retirement."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.1)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        (
            "import sys, time\n"
            "sys.stdout.buffer.write(b'x' * (256 * 1024))\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)\n"
        ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stdin is not None
    retained = client_module.IsolatedFeatureClient(process.stdout, process.stdin)
    transport = process.stdout._transport

    try:
        async with asyncio.timeout(2):
            while not getattr(transport, "_paused", False):
                await asyncio.sleep(0.01)

        wrapper = SubprocessIsolatedFeatureClient(
            command=["unused"], client=retained, process=process
        )
        await wrapper.stop()

        assert transport.is_closing()
        assert not process.stdout._buffer
        assert wrapper._retirements == []
    finally:
        if process.returncode is None:
            await _kill_and_reap_paused_subprocess(process, retained)


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform != "win32", reason="exercises Windows Proactor subprocesses"
)
async def test_windows_real_subprocess_is_terminated_and_reaped(monkeypatch):
    """Windows terminate/reap uses a real asyncio subprocess rather than a double."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.1)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        "import time; print('ready', flush=True); time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert await asyncio.wait_for(process.stdout.readline(), timeout=1) in (
        b"ready\n",
        b"ready\r\n",
    )
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"], process=process)

    try:
        await wrapper.stop()
        assert process.returncode is not None
        assert wrapper.client is None
        assert wrapper.process is None
        assert wrapper._retirements == []
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform != "win32", reason="exercises Windows Proactor streams"
)
async def test_windows_retained_stdout_buffer_is_disposed_before_reap(monkeypatch):
    """A real Windows stdout transport and StreamReader buffer are released."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.1)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        "import sys, time; sys.stdout.buffer.write(b'x' * 262144); sys.stdout.flush(); time.sleep(60)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stdin is not None
    retained = client_module.IsolatedFeatureClient(process.stdout, process.stdin)
    transport = process.stdout._transport

    try:
        async with asyncio.timeout(2):
            while not process.stdout._buffer:
                await asyncio.sleep(0.01)

        wrapper = SubprocessIsolatedFeatureClient(
            command=["unused"], client=retained, process=process
        )
        await wrapper.stop()

        assert transport.is_closing()
        assert not process.stdout._buffer
        assert wrapper._retirements == []
    finally:
        if process.returncode is None:
            await _kill_and_reap_paused_subprocess(process, retained)


@pytest.mark.asyncio
async def test_successful_close_replaces_terminal_traceback_and_still_fails_cleanly():
    """Coordinated close must not retain child event frames through _closed_exc."""

    reader = _QueuedReader()
    client = client_module.IsolatedFeatureClient(reader, _CloseTrackingWriter())
    entered = asyncio.Event()
    secret = "traceback-only-secret"

    async def handler(params):
        assert params["payload"]["token"] == secret
        entered.set()
        await asyncio.Event().wait()

    client.on_event(handler)
    await client.start()
    reader.feed(
        encode_message(
            JsonRpcNotification(
                method=FEATURE_EVENT, params={"payload": {"token": secret}}
            )
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)

    await client.close()

    assert client._read_task is None
    assert isinstance(client._closed_exc, ConnectionError)
    assert client._closed_exc.__traceback__ is None
    assert client._closed_exc.__cause__ is None
    assert client._closed_exc.__context__ is None
    with pytest.raises(ConnectionError, match="isolated feature client is closed"):
        await client.request(HEALTH, {"token": "later-request-secret"})
    assert client._read_task is None
    assert client._closed_exc.__traceback__ is None


@pytest.mark.asyncio
async def test_terminal_sentinel_drops_event_and_later_request_secrets_before_close():
    """Read-loop terminal state never retains event or later request frames."""

    reader = _QueuedReader()
    client = client_module.IsolatedFeatureClient(reader, _CloseTrackingWriter())
    event_secret = "event-secret-before-close"
    request_secret = "request-secret-after-terminal"
    delivered = asyncio.Event()

    async def failing_handler(params):
        assert params["payload"]["token"] == event_secret
        delivered.set()
        raise RuntimeError("event handler failed")

    client.on_event(failing_handler)
    await client.start()
    reader.feed(
        encode_message(
            JsonRpcNotification(
                method=FEATURE_EVENT, params={"payload": {"token": event_secret}}
            )
        )
    )
    await asyncio.wait_for(delivered.wait(), timeout=1)
    assert client._read_task is not None
    await asyncio.wait_for(client._read_task, timeout=1)

    sentinel = client._closed_exc
    assert isinstance(sentinel, ConnectionError)
    assert sentinel.__traceback__ is None
    assert sentinel.__cause__ is None
    assert sentinel.__context__ is None
    assert event_secret not in repr(sentinel)

    with pytest.raises(ConnectionError) as raised:
        await client.request(HEALTH, {"token": request_secret})
    assert raised.value is not sentinel
    assert request_secret not in repr(sentinel)
    assert sentinel.__traceback__ is None
    assert sentinel.__cause__ is None
    assert sentinel.__context__ is None

    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("awaitable_kind", ["task", "future"])
async def test_close_owns_task_and_future_returned_by_event_handler(awaitable_kind):
    """Every EventHandler Awaitable remains joined under the live read owner."""

    reader = _QueuedReader()
    client = client_module.IsolatedFeatureClient(reader, _CloseTrackingWriter())
    created = asyncio.Event()
    returned: asyncio.Future[None] | asyncio.Task[None] | None = None

    async def worker() -> None:
        await asyncio.Event().wait()

    def handler(params):
        nonlocal returned
        if awaitable_kind == "task":
            returned = asyncio.create_task(worker())
        else:
            returned = asyncio.get_running_loop().create_future()
        created.set()
        return returned

    client.on_event(handler)
    await client.start()
    reader.feed(
        encode_message(JsonRpcNotification(method=FEATURE_EVENT, params={"n": 1}))
    )
    await asyncio.wait_for(created.wait(), timeout=1)
    assert returned is not None and not returned.done()

    await client.close()

    assert returned.done()
    assert returned.cancelled()
    assert client._read_task is None


@pytest.mark.asyncio
async def test_buffered_event_task_consumes_failure_and_clears_traceback(caplog):
    """Background buffered delivery retrieves failures without logging payloads."""

    caplog.set_level(logging.WARNING, logger=client_module.__name__)
    client = client_module.IsolatedFeatureClient(
        _TransportOnlyReader(), _CloseTrackingWriter()
    )
    secret = "buffered-handler-secret"
    delivered = asyncio.Event()
    client._pending_notifications.append({"payload": {"token": secret}})

    async def failing_handler(params):
        assert params["payload"]["token"] == secret
        delivered.set()
        raise RuntimeError("handler failure")

    client.on_event(failing_handler)
    await asyncio.wait_for(delivered.wait(), timeout=1)
    await asyncio.sleep(0)

    assert not client._event_tasks
    assert "isolated feature event delivery failed" in caplog.text
    assert secret not in caplog.text
    await client.close()


@pytest.mark.asyncio
async def test_start_retires_dead_real_child_before_starting_replacement():
    """A returncode-0 real child cannot make a later start report false success."""

    dead = await asyncio.create_subprocess_exec(sys.executable, "-c", "")
    await dead.wait()
    service = (
        "import json, sys\n"
        "for raw in sys.stdin:\n"
        "    message = json.loads(raw)\n"
        "    method = message['method']\n"
        "    if method == 'initialize':\n"
        f"        result = {{'protocolVersion': {PROTOCOL_VERSION!r}, "
        "'serverInfo': {}, 'capabilities': {}}\n"
        "    elif method == 'health':\n"
        "        result = {'status': 'ready', 'ready': True}\n"
        "    elif method == 'tools/list':\n"
        "        result = {'tools': []}\n"
        "    else:\n"
        "        result = {}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], "
        "'result': result}), flush=True)\n"
    )
    wrapper = SubprocessIsolatedFeatureClient(
        command=[sys.executable, "-u", "-c", service], process=dead
    )

    try:
        await wrapper.start()
        assert wrapper.process is not dead
        assert wrapper.process is not None and wrapper.process.returncode is None
        assert wrapper.client is not None
        assert wrapper.client.ready is True
        assert wrapper._retirements == []
    finally:
        await wrapper.stop()


@pytest.mark.asyncio
async def test_start_retires_eof_client_with_still_live_process_before_replacement(
    monkeypatch,
):
    """EOF must retire a live process instead of returning its dead client."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    reader = _QueuedReader()
    retained = client_module.IsolatedFeatureClient(reader, _CloseTrackingWriter())
    await retained.start()
    reader.feed(b"")
    assert retained._read_task is not None
    await asyncio.wait_for(retained._read_task, timeout=1)
    assert isinstance(retained._closed_exc, EOFError)

    process = _GracefulProcess()
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=retained, process=process
    )
    spawned = False

    async def replacement_spawn(*args, **kwargs):
        nonlocal spawned
        spawned = True
        raise RuntimeError("replacement start admitted")

    monkeypatch.setattr(
        client_module.asyncio, "create_subprocess_exec", replacement_spawn
    )

    with pytest.raises(RuntimeError, match="replacement start admitted"):
        await wrapper.start()

    assert spawned is True
    assert process.returncode == 0
    assert retained._close_task is not None and retained._close_task.done()
    assert wrapper.client is None
    assert wrapper.process is None
    assert wrapper._retirements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("wedged_phase", ["initialize", "health", "sleep", "tools"])
async def test_startup_deadline_retires_child_for_every_wedged_phase(
    monkeypatch, wedged_phase
):
    """Initialize, health, retry sleep, and tools/list share one timeout path."""

    process = _ExitedProcess()
    reader = _TransportOnlyReader()
    writer = _CloseTrackingWriter()

    async def spawn(*args, **kwargs):
        process.stdin = writer
        process.stdout = reader
        return process

    async def initialize(self, *args, **kwargs):
        if wedged_phase == "initialize":
            await asyncio.Event().wait()
        return {}

    async def health(self):
        if wedged_phase == "health":
            await asyncio.Event().wait()
        return {"ready": False} if wedged_phase == "sleep" else {"ready": True}

    async def list_tools(self):
        if wedged_phase == "tools":
            await asyncio.Event().wait()
        return []

    async def shutdown(self):
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "initialize", initialize)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "health", health)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "list_tools", list_tools)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "shutdown", shutdown)
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"], ready_timeout=0.03)

    with pytest.raises(TimeoutError):
        await wrapper.start()

    assert wrapper.client is None
    assert wrapper.process is None
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_startup_deadline_retains_cancellation_suppressing_phase(monkeypatch):
    """A startup timeout cannot wait forever or forget its exact phase task."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    process = _ExitedProcess()
    reader = _TransportOnlyReader()
    writer = _CloseTrackingWriter()
    entered = asyncio.Event()
    release = asyncio.Event()
    initialize_calls = 0
    initialize_cancellations = 0

    async def spawn(*args, **kwargs):
        process.stdin = writer
        process.stdout = reader
        return process

    async def stubborn_initialize(self, *args, **kwargs):
        nonlocal initialize_calls, initialize_cancellations
        initialize_calls += 1
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                initialize_cancellations += 1
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        client_module.IsolatedFeatureClient, "initialize", stubborn_initialize
    )
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"], ready_timeout=0.03)

    started_at = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await wrapper.start()
    elapsed = asyncio.get_running_loop().time() - started_at

    assert elapsed < 0.15
    assert entered.is_set()
    assert initialize_calls == 1
    assert initialize_cancellations == 1
    retirement = wrapper._retirements[0]
    assert retirement.client is not None
    assert len(retirement.startup_tasks) == 1
    phase_task = next(iter(retirement.startup_tasks))
    assert not phase_task.done()

    release.set()
    await wrapper.stop()
    assert phase_task.done()
    assert initialize_calls == 1
    assert wrapper._retirements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_phase", (False, True), ids=("result", "exception"))
async def test_late_startup_phase_releases_payload_without_stop_retry(
    monkeypatch,
    fail_phase,
):
    """A late startup completion drops its task while its process stays fenced."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.01)
    process = _NeverReapedProcess()
    reader = _TransportOnlyReader()
    writer = _CloseTrackingWriter()
    release = asyncio.Event()
    entered = asyncio.Event()
    phase_calls = 0
    phase_cancellations = 0
    secret: _SecretPayload | None = _SecretPayload(token="secret")
    secret_ref = weakref.ref(secret)

    async def spawn(*args, **kwargs):
        process.stdin = writer
        process.stdout = reader
        return process

    async def stubborn_initialize(self, *args, **kwargs):
        nonlocal phase_calls, phase_cancellations, secret
        phase_calls += 1
        payload = secret
        secret = None
        assert payload is not None
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                phase_cancellations += 1
        if fail_phase:
            raise RuntimeError(payload)
        return payload

    async def successful_shutdown(self):
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        client_module.IsolatedFeatureClient, "initialize", stubborn_initialize
    )
    monkeypatch.setattr(
        client_module.IsolatedFeatureClient, "shutdown", successful_shutdown
    )
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"], ready_timeout=0.01)

    with pytest.raises(TimeoutError):
        await wrapper.start()
    assert entered.is_set()
    assert phase_calls == 1
    assert phase_cancellations == 1
    retirement = wrapper._retirements[0]
    assert len(retirement.startup_tasks) == 1
    assert retirement.startup_attempted == 1
    assert retirement.startup_settled == 0
    assert process.terminate_calls == 1
    assert process.kill_calls == 1

    release.set()
    async with asyncio.timeout(1):
        while retirement.startup_tasks:
            await asyncio.sleep(0)

    assert retirement.startup_settled == 1
    assert retirement.startup_succeeded == int(not fail_phase)
    assert phase_calls == 1
    assert phase_cancellations == 1
    gc.collect()
    assert secret_ref() is None

    # Completion releases the late phase data but cannot release the exact
    # process record or admit another generation before its waiter settles.
    with pytest.raises(RuntimeError, match="retirement is in progress"):
        await wrapper.start()

    process.finish()
    await wrapper.stop()
    assert phase_calls == 1
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_independently_cancelled_spawn_is_generic_startup_failure(monkeypatch):
    """A cancelled owned spawn is not reported as cancellation of start()."""

    entered = asyncio.Event()

    async def blocked_spawn(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", blocked_spawn)
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"])
    start_task = asyncio.create_task(wrapper.start())
    await asyncio.wait_for(entered.wait(), timeout=1)
    spawn_task = wrapper._spawn_task
    assert spawn_task is not None
    spawn_task.cancel("independent-spawn-cancellation")

    with pytest.raises(RuntimeError, match="startup phase was cancelled"):
        await start_task

    assert not start_task.cancelled()
    assert start_task.cancelling() == 0
    assert wrapper.client is None
    assert wrapper.process is None
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_independently_cancelled_startup_phase_is_generic_and_fully_retired(
    monkeypatch,
):
    """A cancelled initialize task retains cleanup ownership but not caller cancellation."""

    process = _ExitedProcess()
    entered = asyncio.Event()

    async def spawn(*args, **kwargs):
        process.stdin = _CloseTrackingWriter()
        process.stdout = _TransportOnlyReader()
        return process

    async def blocked_initialize(self, *args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def successful_shutdown(self):
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        client_module.IsolatedFeatureClient, "initialize", blocked_initialize
    )
    monkeypatch.setattr(
        client_module.IsolatedFeatureClient, "shutdown", successful_shutdown
    )
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"])
    start_task = asyncio.create_task(wrapper.start())
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert len(wrapper._startup_tasks) == 1
    phase_task = next(iter(wrapper._startup_tasks))
    phase_task.cancel("independent-initialize-cancellation")

    with pytest.raises(RuntimeError, match="startup phase was cancelled"):
        await start_task

    assert not start_task.cancelled()
    assert start_task.cancelling() == 0
    assert phase_task.done()
    assert wrapper.client is None
    assert wrapper.process is None
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_startup_deadline_is_not_reset_after_initialize(monkeypatch):
    """A slow initialize consumes the same deadline later health observes."""

    process = _ExitedProcess()
    reader = _TransportOnlyReader()
    writer = _CloseTrackingWriter()
    health_started = asyncio.Event()

    async def spawn(*args, **kwargs):
        process.stdin = writer
        process.stdout = reader
        return process

    async def slow_initialize(self, *args, **kwargs):
        await asyncio.sleep(0.04)
        return {}

    async def wedged_health(self):
        health_started.set()
        await asyncio.Event().wait()

    async def shutdown(self):
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        client_module.IsolatedFeatureClient, "initialize", slow_initialize
    )
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "health", wedged_health)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "shutdown", shutdown)
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"], ready_timeout=0.06)

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    with pytest.raises(TimeoutError):
        await wrapper.start()
    elapsed = loop.time() - started_at

    assert health_started.is_set()
    assert elapsed < 0.1
    assert wrapper.client is None
    assert wrapper.process is None
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_startup_phase_is_owned_before_state_lock_can_block_transfer(
    monkeypatch,
):
    """A phase never exists live between create_task() and retirement ownership."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    process = _ExitedProcess()
    entered = asyncio.Event()
    release = asyncio.Event()
    spawn_calls = 0

    async def spawn(*args, **kwargs):
        nonlocal spawn_calls
        spawn_calls += 1
        if spawn_calls > 1:
            raise AssertionError("replacement was admitted while phase was live")
        process.stdin = _CloseTrackingWriter()
        process.stdout = _TransportOnlyReader()
        return process

    async def stubborn_initialize(self, *args, **kwargs):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        client_module.IsolatedFeatureClient, "initialize", stubborn_initialize
    )
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"])
    start_task = asyncio.create_task(wrapper.start())
    await asyncio.wait_for(entered.wait(), timeout=1)

    assert len(wrapper._startup_tasks) == 1
    phase_task = next(iter(wrapper._startup_tasks))
    assert not phase_task.done()

    # This is the old attachment boundary: cancellation has to queue behind a
    # held state lock. The phase must already be synchronously owned, so stop
    # cannot report success or let a replacement through in that interval.
    await wrapper._state_lock.acquire()
    try:
        start_task.cancel("phase-owner-cancel")
        await asyncio.sleep(0)
        stop_task = asyncio.create_task(wrapper.stop())
        await asyncio.sleep(0)
        assert phase_task in wrapper._startup_tasks
    finally:
        wrapper._state_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(start_task, timeout=1)
    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await asyncio.wait_for(stop_task, timeout=1)

    assert any(phase_task in item.startup_tasks for item in wrapper._retirements)
    with pytest.raises(RuntimeError, match="retirement is in progress"):
        await wrapper.start()
    assert spawn_calls == 1

    release.set()
    await wrapper.stop()
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_event_exception_group_cleanup_clears_nested_payload_tracebacks():
    """Nested ExceptionGroup members cannot retain a handler event payload."""

    reader = _QueuedReader()
    client = client_module.IsolatedFeatureClient(reader, _CloseTrackingWriter())
    secret = "nested-exception-group-event-secret"
    returned: asyncio.Task[None] | None = None
    delivered = asyncio.Event()

    def handler(params):
        nonlocal returned

        async def fail_group() -> None:
            payload = params
            try:
                assert payload["payload"]["token"] == secret
                raise RuntimeError("nested handler failure")
            except RuntimeError as nested:
                raise ExceptionGroup("handler failure", [nested])

        returned = asyncio.create_task(fail_group())
        delivered.set()
        return returned

    client.on_event(handler)
    await client.start()
    reader.feed(
        encode_message(
            JsonRpcNotification(
                method=FEATURE_EVENT,
                params={"payload": {"token": secret}},
            )
        )
    )
    await asyncio.wait_for(delivered.wait(), timeout=1)
    assert client._read_task is not None
    await asyncio.wait_for(client._read_task, timeout=1)
    assert returned is not None

    group = returned.exception()
    assert isinstance(group, BaseException)
    pending = [group]
    while pending:
        current = pending.pop()
        assert current.__traceback__ is None
        assert current.__cause__ is None
        assert current.__context__ is None
        nested = getattr(current, "exceptions", ())
        pending.extend(item for item in nested if isinstance(item, BaseException))

    await client.close()


@pytest.mark.asyncio
async def test_hostile_exception_group_property_cannot_hide_nested_traceback():
    """The native ExceptionGroup descriptor still exposes nested members to scrub."""

    reader = _QueuedReader()
    client = client_module.IsolatedFeatureClient(reader, _CloseTrackingWriter())
    secret = "hostile-exception-group-event-secret"
    returned: asyncio.Task[None] | None = None
    delivered = asyncio.Event()

    def handler(params):
        nonlocal returned

        async def fail_group() -> None:
            payload = params
            try:
                assert payload["payload"]["token"] == secret
                raise RuntimeError("nested handler failure")
            except RuntimeError as nested:
                raise _HostileExceptionsPropertyGroup("hostile group", [nested])

        returned = asyncio.create_task(fail_group())
        delivered.set()
        return returned

    client.on_event(handler)
    await client.start()
    reader.feed(
        encode_message(
            JsonRpcNotification(
                method=FEATURE_EVENT, params={"payload": {"token": secret}}
            )
        )
    )
    await asyncio.wait_for(delivered.wait(), timeout=1)
    assert client._read_task is not None
    await asyncio.wait_for(client._read_task, timeout=1)
    assert returned is not None

    group = returned.exception()
    assert isinstance(group, BaseExceptionGroup)
    nested = BaseExceptionGroup.exceptions.__get__(group, type(group))
    assert len(nested) == 1
    assert group.__traceback__ is None
    assert nested[0].__traceback__ is None
    assert nested[0].__cause__ is None
    assert nested[0].__context__ is None

    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["done", "set_exception"])
async def test_terminal_pending_completion_failures_are_sanitized_and_do_not_strand_waiters(
    failure_point,
):
    """Hostile pending completion hooks cannot retain terminal reader frames."""

    reader = _QueuedReader()
    client = client_module.IsolatedFeatureClient(reader, _CloseTrackingWriter())
    hostile_pending = _FailingPendingCompletion(failure_point)
    normal_pending = asyncio.get_running_loop().create_future()
    client._pending[1] = hostile_pending  # type: ignore[assignment]
    client._pending[2] = normal_pending
    secret = "terminal-event-secret-for-pending-cleanup"

    def handler(params):
        assert params["payload"]["token"] == secret
        raise RuntimeError("feature event failed")

    client.on_event(handler)
    await client.start()
    reader.feed(
        encode_message(
            JsonRpcNotification(
                method=FEATURE_EVENT, params={"payload": {"token": secret}}
            )
        )
    )
    assert client._read_task is not None
    await asyncio.wait_for(client._read_task, timeout=1)

    assert hostile_pending.error is not None
    assert hostile_pending.error.__traceback__ is None
    assert client._pending == {}
    assert normal_pending.done()
    assert isinstance(normal_pending.exception(), ConnectionError)

    await client.close()


@pytest.mark.asyncio
async def test_hostile_handler_exception_cannot_strand_pending_request_cleanup():
    """Overridden exception attributes cannot abort reader terminal cleanup."""

    reader = _QueuedReader()
    writer = _SilentRequestWriter()
    client = client_module.IsolatedFeatureClient(reader, writer)
    event_secret = "hostile-handler-event-secret"
    request_secret = "concurrent-request-secret"
    raised: _HostileCauseException | None = None

    async def handler(params):
        nonlocal raised
        payload = params
        assert payload["payload"]["token"] == event_secret
        raised = _HostileCauseException("feature failure")
        raise raised

    client.on_event(handler)
    request_task = asyncio.create_task(
        client.request("secret/request", {"token": request_secret})
    )
    async with asyncio.timeout(1):
        while not client._pending:
            await asyncio.sleep(0)
    pending = next(iter(client._pending.values()))
    reader.feed(
        encode_message(
            JsonRpcNotification(
                method=FEATURE_EVENT,
                params={"payload": {"token": event_secret}},
            )
        )
    )

    assert client._read_task is not None
    await asyncio.wait_for(client._read_task, timeout=1)
    assert raised is not None
    assert BaseException.__getattribute__(raised, "__traceback__") is None
    assert client._pending == {}
    assert pending.done()
    assert isinstance(pending.exception(), ConnectionError)
    assert isinstance(client._closed_exc, ConnectionError)
    assert client._read_task.exception() is None
    assert client._read_task.get_coro().cr_frame is None
    with pytest.raises(ConnectionError):
        await request_task
    await asyncio.wait_for(client.close(), timeout=1)


@pytest.mark.asyncio
async def test_non_cancellation_base_exception_is_sanitized_at_event_boundary():
    """A bare feature BaseException becomes a generic reader terminal state."""

    reader = _QueuedReader()
    writer = _SilentRequestWriter()
    client = client_module.IsolatedFeatureClient(reader, writer)
    secret = "bare-base-exception-event-secret"
    raised: _NonCancellationFeatureExit | None = None

    def handler(params):
        nonlocal raised
        payload = params
        assert payload["payload"]["token"] == secret
        raised = _NonCancellationFeatureExit("feature exit")
        raise raised

    client.on_event(handler)
    request_task = asyncio.create_task(client.request("pending/request"))
    async with asyncio.timeout(1):
        while not client._pending:
            await asyncio.sleep(0)
    pending = next(iter(client._pending.values()))
    reader.feed(
        encode_message(
            JsonRpcNotification(
                method=FEATURE_EVENT,
                params={"payload": {"token": secret}},
            )
        )
    )

    assert client._read_task is not None
    await asyncio.wait_for(client._read_task, timeout=1)
    assert raised is not None
    assert BaseException.__getattribute__(raised, "__traceback__") is None
    assert client._pending == {}
    assert pending.done()
    assert isinstance(pending.exception(), ConnectionError)
    assert isinstance(client._closed_exc, ConnectionError)
    assert client._read_task.exception() is None
    assert client._read_task.get_coro().cr_frame is None
    with pytest.raises(ConnectionError):
        await request_task
    await asyncio.wait_for(client.close(), timeout=1)


@pytest.mark.asyncio
async def test_spawn_deadline_fences_replacement_and_reaps_late_process(monkeypatch):
    """A late cancellation-suppressing spawn is owned, reaped, and released."""

    monkeypatch.setattr(client_module, "_SUBPROCESS_STOP_TIMEOUT", 0.03)
    entered = asyncio.Event()
    release = asyncio.Event()
    process = _LateSpawnProcess()
    spawn_calls = 0
    spawn_cancellations = 0
    config = {"credential": "late-spawn-config-secret"}

    async def stubborn_spawn(*args, **kwargs):
        nonlocal spawn_calls, spawn_cancellations
        spawn_calls += 1
        if spawn_calls > 1:
            raise AssertionError("replacement spawn must remain fenced")
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                spawn_cancellations += 1
                current = asyncio.current_task()
                assert current is not None
                current.uncancel()
        return process

    async def successful_shutdown(self):
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", stubborn_spawn)
    monkeypatch.setattr(
        client_module.IsolatedFeatureClient, "shutdown", successful_shutdown
    )
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"],
        ready_timeout=0.03,
        config=config,
    )

    started_at = asyncio.get_running_loop().time()
    start_task = asyncio.create_task(wrapper.start())
    await asyncio.wait_for(entered.wait(), timeout=1)
    with pytest.raises(TimeoutError):
        await start_task
    assert asyncio.get_running_loop().time() - started_at < 0.15

    assert spawn_cancellations == 1
    assert wrapper.client is None
    assert wrapper.process is None
    assert len(wrapper._retirements) == 1
    retirement = wrapper._retirements[0]
    spawn_task = retirement.spawn_task
    assert spawn_task is not None and not spawn_task.done()

    # Repeated stop observes the same retained spawn and cannot inflate its
    # cancellation count or falsely report that no child remains.
    with pytest.raises(RuntimeError, match="retirement is unresolved"):
        await wrapper.stop()
    assert spawn_cancellations == 1
    with pytest.raises(RuntimeError, match="retirement is in progress"):
        await wrapper.start()
    assert spawn_calls == 1

    # Completion happens after public start/stop returned. The task callback
    # must privately adopt this exact process, close its streams, terminate and
    # reap it, then drop the task that held the startup configuration frame.
    release.set()
    async with asyncio.timeout(1):
        while wrapper._retirements:
            await asyncio.sleep(0)

    assert process.returncode == 0
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_calls == 1
    assert process.stdin.close_calls == 1
    assert process.stdout._transport.close_calls == 1
    assert retirement.spawn_task is None
    assert retirement.client is None
    assert spawn_task.get_coro().cr_frame is None
    assert wrapper.config is config


@pytest.mark.asyncio
async def test_stop_accepts_legacy_close_completion_returning_none():
    """The public close()->None contract is a successful retirement outcome."""

    legacy = _LegacyCloseClient()
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], client=legacy, process=_ExitedProcess()
    )

    await wrapper.stop()

    assert legacy.close_calls == 1
    assert wrapper.client is None
    assert wrapper.process is None
    assert wrapper._retirements == []


@pytest.mark.asyncio
async def test_start_reconciles_handler_registered_in_publication_gap(monkeypatch):
    """A handler added while start publishes is attached once before initialize."""

    spawned = asyncio.Event()
    release_spawn = asyncio.Event()
    delivered = asyncio.Event()
    reader = _QueuedReader()
    process = _GracefulProcess()
    writer = _CloseTrackingWriter()

    async def spawn(*args, **kwargs):
        process.stdin = writer
        process.stdout = reader
        spawned.set()
        await release_spawn.wait()
        return process

    def handler(params):
        if params["payload"] == "publication-gap":
            delivered.set()

    async def initialize(self, *args, **kwargs):
        assert self._event_handlers.count(handler) == 1
        await self.start()
        reader.feed(
            encode_message(
                JsonRpcNotification(
                    method=FEATURE_EVENT, params={"payload": "publication-gap"}
                )
            )
        )
        return {}

    async def health(self):
        return {"ready": True}

    async def list_tools(self):
        return []

    async def shutdown(self):
        return {}

    monkeypatch.setattr(client_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "initialize", initialize)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "health", health)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "list_tools", list_tools)
    monkeypatch.setattr(client_module.IsolatedFeatureClient, "shutdown", shutdown)
    wrapper = SubprocessIsolatedFeatureClient(command=["unused"])
    start_task = asyncio.create_task(wrapper.start())

    await asyncio.wait_for(spawned.wait(), timeout=1)
    await wrapper._state_lock.acquire()
    try:
        release_spawn.set()
        await asyncio.sleep(0)
        wrapper.on_event(handler)
    finally:
        wrapper._state_lock.release()

    try:
        await asyncio.wait_for(start_task, timeout=1)
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert wrapper.client is not None
        assert wrapper.client._event_handlers.count(handler) == 1
    finally:
        await wrapper.stop()


@pytest.mark.parametrize("mutation", ["retarget", "delete"])
def test_local_final_release_revalidation_rejects_moved_or_deleted_tag(
    tmp_path: Path, mutation: str
):
    """A stale checkout cannot make the final remote tag check pass."""

    source, verifier, tag = _create_local_release_remote(tmp_path)
    resolved_sha = _resolve_local_release_tag(verifier, tag)

    if mutation == "retarget":
        _git(source, "commit", "--allow-empty", "-m", "retarget release tag")
        _git(source, "tag", "-fa", tag, "-m", "retarget")
        _git(source, "push", "--force", "origin", f"refs/tags/{tag}")
    else:
        _git(source, "push", "origin", f":refs/tags/{tag}")

    assert not _final_revalidate_local_release_tag(verifier, tag, resolved_sha)


def test_publish_workflow_final_revalidation_graph_and_fail_closed_contract():
    """Keep the non-OIDC gate and every publish needs reference explicit."""

    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"
    ).read_text()
    final_revalidate = workflow.split("\n  final-revalidate:\n", 1)[1].split(
        "\n  publish:\n", 1
    )[0]
    publish = workflow.split("\n  publish:\n", 1)[1]

    assert "needs: [resolve-release, ci, build, artifact-test]" in final_revalidate
    assert "contents: read" in final_revalidate
    assert "id-token:" not in final_revalidate
    assert "validated: ${{ steps.revalidate.outputs.validated }}" in final_revalidate
    assert (
        'git ls-remote --exit-code --refs origin "$RELEASE_REF" >/dev/null'
        in final_revalidate
    )
    assert (
        'git fetch --force --no-tags origin "+$RELEASE_REF:$RELEASE_REF"'
        in final_revalidate
    )
    assert 'git rev-parse --verify "${RELEASE_REF}^{commit}"' in final_revalidate
    assert "printf 'validated=true\\n' >> \"$GITHUB_OUTPUT\"" in final_revalidate

    declared_needs = {
        "resolve-release",
        "ci",
        "build",
        "artifact-test",
        "final-revalidate",
    }
    assert (
        "needs: [resolve-release, ci, build, artifact-test, final-revalidate]"
        in publish
    )
    referenced_needs = set(re.findall(r"needs\.([a-z-]+)\.", publish))
    assert referenced_needs <= declared_needs
    assert "needs.final-revalidate.result == 'success'" in publish
    assert "needs.final-revalidate.outputs.validated == 'true'" in publish

    # Expressions enter shell only through explicitly named environment values.
    for shell_body in re.findall(
        r"(?m)^        run: \|\n((?:^          [^\n]*\n?)*)", workflow
    ):
        assert "${{" not in shell_body


def test_release_workflow_authorization_toolchain_and_upload_adjacency_contract():
    """Keep external release controls and reviewed build inputs explicit."""

    root = Path(__file__).resolve().parents[1]
    ci_workflow = (root / ".github" / "workflows" / "ci.yml").read_text()
    publish_workflow = (root / ".github" / "workflows" / "publish.yml").read_text()
    pyproject = (root / "pyproject.toml").read_text()

    # These are external GitHub settings, not workflow permissions. Retain the
    # exact review contract in source so a workflow review cannot mistake the
    # remote recheck for creation authorization or environment approval.
    for contract in (
        "an active GitHub repository ruleset must cover release-tag creation, update,",
        "and deletion for refs/tags/v*",
        "A creation bypass may be granted only to",
        "tightly scoped release principals; update and deletion must have no bypass.",
        "The pypi environment must independently require approval",
        "no workflow-only check can replace those controls",
    ):
        assert contract in publish_workflow
    assert 'git merge-base --is-ancestor "$TAG_SHA" origin/main' in publish_workflow
    assert "cannot authorize tag creation or replace repository" in publish_workflow

    # The release graph passes only the resolved immutable source SHA into CI
    # and every upload dependency is declared directly, not inferred.
    assert (
        "needs: resolve-release\n    uses: ./.github/workflows/ci.yml"
        in publish_workflow
    )
    assert "source_sha: ${{ needs.resolve-release.outputs.sha }}" in publish_workflow
    assert "needs: [resolve-release, ci]" in publish_workflow
    assert "needs: build\n    runs-on: ubuntu-latest" in publish_workflow
    assert "needs: [resolve-release, ci, build, artifact-test]" in publish_workflow
    assert (
        "needs: [resolve-release, ci, build, artifact-test, final-revalidate]"
        in publish_workflow
    )

    assert '[build-system]\nrequires = ["hatchling==1.31.0"]' in pyproject
    expected_constraints = {
        "hatchling==1.31.0": (
            "6b48ad4068a482ed7239b3a8215bc55b47aad3345d58dfc94e553c5d2d46211b",
            "aac80bec8b6fe35e8480f1c335be8910fa210a0e6f735a139be205dadcacb544",
        ),
        "packaging==26.2": (
            "5fc45236b9446107ff2415ce77c807cee2862cb6fac22b8a73826d0693b0980e",
            "ff452ff5a3e828ce110190feff1178bb1f2ea2281fa2075aadb987c2fb221661",
        ),
        "pathspec==1.1.1": (
            "17db5ecd524104a120e173814c90367a96a98d07c45b2e10c2f3919fff91bf5a",
            "a00ce642f577bf7f473932318056212bc4f8bfdf53128c78bbd5af0b9b20b189",
        ),
        "pluggy==1.6.0": (
            "7dcc130b76258d33b90f61b658791dede3486c3e6bfb003ee5c9bfb396dd22f3",
            "e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746",
        ),
        "trove-classifiers==2026.6.1.19": (
            "ab4c4ec93cc4a4e7815fa759906e05e6bb3f2fbd92ea0f897288c6a43efd15b3",
            "c5132b4b61a829d11cfbd2d72e97f20a45ed6edb95e45c5efdeb5e00836b2745",
        ),
    }
    for workflow in (ci_workflow, publish_workflow):
        setup_uv_blocks = re.findall(
            r"uses: astral-sh/setup-uv@[^\n]+\n\s+with:\n\s+version: \"0\.9\.22\"",
            workflow,
        )
        assert len(setup_uv_blocks) == workflow.count("uses: astral-sh/setup-uv@")
        assert workflow.count("cat > \"$BUILD_CONSTRAINTS\" <<'EOF'") == 1
        assert (
            workflow.count('--build-constraints "$BUILD_CONSTRAINTS" --require-hashes')
            == 1
        )
        assert workflow.count("uv build --wheel --sdist --clear --force-pep517") == 1
        for requirement, hashes in expected_constraints.items():
            assert workflow.count(requirement) == 1
            for digest in hashes:
                assert workflow.count(f"--hash=sha256:{digest}") == 1

        # GitHub expressions may populate named environment variables, never a
        # shell body. The checks include every multiline runner script here.
        for shell_body in re.findall(
            r"(?m)^        run: \|\n((?:^          [^\n]*\n?)*)", workflow
        ):
            assert "${{" not in shell_body

    # Wheel inspection is intentionally limited to the standard-library
    # archive check below; an ad-hoc `uv run --with` graph is neither locked
    # nor hash-constrained.
    assert "check-wheel-contents" not in ci_workflow
    assert "uv run --with" not in ci_workflow
    assert "import zipfile, sys" in ci_workflow
    assert "z = zipfile.ZipFile('$WHEEL')" in ci_workflow
    assert "Wheel OK ({len(names)} entries)" in ci_workflow

    # The upload remains native to the exact uv binary installed by a
    # SHA-pinned action. Do not reintroduce a container action whose base image
    # can move after PyPI environment approval.
    publish_job = publish_workflow.split("\n  publish:\n", 1)[1]
    assert "pypa/gh-action-pypi-publish@" not in publish_workflow
    assert "python:3.12-slim" not in publish_workflow
    assert (
        "uses: astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78"
        in publish_job
    )
    assert 'version: "0.9.22"' in publish_job
    assert "uv publish --no-config --trusted-publishing always" in publish_job
    assert "--publish-url https://upload.pypi.org/legacy/" in publish_job
    for workflow in (ci_workflow, publish_workflow):
        for action, revision in re.findall(
            r"(?m)^\s*uses:\s+([^@\s]+)@([^\s#]+)", workflow
        ):
            assert re.fullmatch(r"[0-9a-f]{40}", revision), action

    publish_steps = re.findall(r"(?m)^      - name: (.+)$", publish_workflow)
    assert publish_steps.index(
        "Record exact artifact provenance"
    ) < publish_steps.index("Download build artifacts")
    assert publish_steps[-2:] == [
        "Recheck release tag immediately before upload",
        "Publish with uv OIDC trusted publishing",
    ]
