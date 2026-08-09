"""Adversarial regression tests for the isolated-feature stdio adapters."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import traceback

import pytest

from kestrel_sdk.isolated_feature import (
    IsolatedFeatureService,
    JsonRpcNotification,
    encode_message,
)
from kestrel_sdk.isolated_feature import service as service_module


async def _wait_for_thread_event(event: threading.Event) -> None:
    """Bridge a deterministic worker signal without using an executor."""

    for _ in range(1_000):
        if event.is_set():
            return
        await asyncio.sleep(0)
    raise AssertionError("worker did not reach its expected synchronization point")


class _GateableWriteStream:
    """Synchronous stream double that exposes write overlap and close order."""

    def __init__(self) -> None:
        self.write_started = threading.Event()
        self.release_first_write = threading.Event()
        self.closed = threading.Event()
        self.writes: list[bytes] = []
        self.flushes = 0
        self._active_writes = 0
        self.overlapped = False
        self.close_during_write = False

    def write(self, data) -> int:
        self._active_writes += 1
        try:
            if self._active_writes > 1:
                self.overlapped = True
            payload = bytes(data)
            self.writes.append(payload)
            if len(self.writes) == 1:
                self.write_started.set()
                self.release_first_write.wait()
            return len(payload)
        finally:
            self._active_writes -= 1

    def flush(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        self.close_during_write = self._active_writes != 0
        self.closed.set()


class _PartialWriteStream:
    """Synchronous stream double that accepts only small fragments per write."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.flushes = 0
        self.closed = threading.Event()

    def write(self, data) -> int:
        chunk = bytes(data[:2])
        self.chunks.append(chunk)
        return len(chunk)

    def flush(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        self.closed.set()


class _FailingWriteStream:
    """Synchronous stream double that models terminal write failures."""

    def __init__(self, result: int | None) -> None:
        self._result = result
        self.closed = threading.Event()

    def write(self, data) -> int | None:
        return self._result

    def flush(self) -> None:
        raise AssertionError("a failed write must not flush")

    def close(self) -> None:
        self.closed.set()


class _PartialThenFailingWriteStream:
    """Accept part of one frame, then fail before any later frame can write."""

    def __init__(self) -> None:
        self.partial_write = threading.Event()
        self.fail_next_write = threading.Event()
        self.closed = threading.Event()
        self.chunks: list[bytes] = []
        self.write_attempts = 0
        self.failure_secret = "wire failure secret must never escape"

    def write(self, data) -> int:
        self.write_attempts += 1
        if self.write_attempts == 1:
            chunk = bytes(data[:2])
            self.chunks.append(chunk)
            self.partial_write.set()
            self.fail_next_write.wait()
            return len(chunk)
        raise OSError(self.failure_secret)

    def flush(self) -> None:
        raise AssertionError("a failed partial frame must not flush")

    def close(self) -> None:
        self.closed.set()


class _BlockingReadStream:
    """Synchronous stdin double whose first read cannot be interrupted."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def readline(self) -> bytes:
        self.started.set()
        self.release.wait()
        self.finished.set()
        return b"late line\n"


def _assert_payload_free_terminal_error(
    error: BaseException, *, secret: str = ""
) -> None:
    """A terminal wire error must not retain or reveal the stream exception."""

    assert type(error) is ConnectionError
    assert str(error) == "isolated feature stdio writer has a terminal I/O failure"
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    if secret:
        assert secret not in str(error)
        assert secret not in repr(error)
        assert secret not in rendered


@pytest.mark.asyncio
async def test_cancelled_send_keeps_windows_frames_ordered_and_lossless():
    """A cancelled drain cannot let a later sender overlap or overtake its frame."""

    stream = _GateableWriteStream()
    writer = service_module._ThreadedStdioWriter(stream)  # type: ignore[arg-type]
    service = IsolatedFeatureService(name="stdio", version="1.0.0")
    service._writer = writer
    first = JsonRpcNotification(method="first", params={"position": 1})
    second = JsonRpcNotification(method="second", params={"position": 2})

    first_send = asyncio.create_task(service._send(first))
    try:
        await _wait_for_thread_event(stream.write_started)
        first_send.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_send

        # The service lock is released by task cancellation, so this exercises
        # the exact old overlap window rather than merely serial direct drains.
        assert not service._write_lock.locked()
        second_send = asyncio.create_task(service._send(second))
        await asyncio.sleep(0)
        assert stream.writes == [encode_message(first)]
        assert not stream.overlapped

        stream.release_first_write.set()
        await asyncio.wait_for(second_send, timeout=1)

        assert stream.writes == [encode_message(first), encode_message(second)]
        assert not stream.overlapped
        assert stream.flushes == 2
    finally:
        stream.release_first_write.set()
        writer.close()
        await _wait_for_thread_event(stream.closed)


@pytest.mark.asyncio
async def test_windows_writer_close_waits_for_active_write_without_blocking():
    """Close queues behind an active worker instead of closing its stream under it."""

    stream = _GateableWriteStream()
    writer = service_module._ThreadedStdioWriter(stream)  # type: ignore[arg-type]
    writer.write(b"complete frame\n")
    drain = asyncio.create_task(writer.drain())
    try:
        await _wait_for_thread_event(stream.write_started)
        writer.close()

        assert not stream.closed.is_set()
        assert not stream.close_during_write

        stream.release_first_write.set()
        await asyncio.wait_for(drain, timeout=1)
        await _wait_for_thread_event(stream.closed)

        assert stream.writes == [b"complete frame\n"]
        assert not stream.close_during_write
    finally:
        stream.release_first_write.set()
        writer.close()


@pytest.mark.asyncio
async def test_windows_writer_handles_partial_writes_and_reports_terminal_failures():
    """Frames are fully written, while zero/None writes reach the sender as errors."""

    partial = _PartialWriteStream()
    writer = service_module._ThreadedStdioWriter(partial)  # type: ignore[arg-type]
    writer.write(b"abcdef")
    try:
        await asyncio.wait_for(writer.drain(), timeout=1)
        assert b"".join(partial.chunks) == b"abcdef"
        assert partial.flushes == 1
    finally:
        writer.close()
        await _wait_for_thread_event(partial.closed)

    for result in (0, None):
        failing = _FailingWriteStream(result)
        writer = service_module._ThreadedStdioWriter(failing)  # type: ignore[arg-type]
        writer.write(b"frame")
        try:
            with pytest.raises(ConnectionError) as raised:
                await asyncio.wait_for(writer.drain(), timeout=1)
            _assert_payload_free_terminal_error(raised.value)
        finally:
            writer.close()
            await _wait_for_thread_event(failing.closed)


@pytest.mark.asyncio
async def test_windows_writer_partial_frame_failure_fences_queued_and_later_drains():
    """A corrupt partial frame prevents every later frame from reaching the wire."""

    stream = _PartialThenFailingWriteStream()
    writer = service_module._ThreadedStdioWriter(stream)  # type: ignore[arg-type]
    first_frame = b'{"id":1}\n'
    later_frame = b'{"id":2}\n'
    writer.write(first_frame)
    first_drain = asyncio.create_task(writer.drain())
    try:
        await _wait_for_thread_event(stream.partial_write)
        writer.write(later_frame)
        later_drain = asyncio.create_task(writer.drain())

        for _ in range(1_000):
            with writer._state_lock:
                if len(writer._drains) == 2:
                    break
            await asyncio.sleep(0)
        else:
            raise AssertionError("later drain was not queued behind the partial frame")

        stream.fail_next_write.set()
        with pytest.raises(ConnectionError) as first_error:
            await asyncio.wait_for(first_drain, timeout=1)
        with pytest.raises(ConnectionError) as later_error:
            await asyncio.wait_for(later_drain, timeout=1)

        _assert_payload_free_terminal_error(
            first_error.value, secret=stream.failure_secret
        )
        _assert_payload_free_terminal_error(
            later_error.value, secret=stream.failure_secret
        )
        assert first_error.value is not later_error.value

        assert stream.chunks == [first_frame[:2]]
        assert stream.write_attempts == 2
        assert writer._jobs.empty()
        assert writer._pending == bytearray()
        assert not writer._drains
        with pytest.raises(ConnectionError) as write_error:
            writer.write(b'{"id":3}\n')
        with pytest.raises(ConnectionError) as drain_error:
            await writer.drain()
        _assert_payload_free_terminal_error(
            write_error.value, secret=stream.failure_secret
        )
        _assert_payload_free_terminal_error(
            drain_error.value, secret=stream.failure_secret
        )
        assert write_error.value is not drain_error.value
    finally:
        stream.fail_next_write.set()
        writer.close()
        await _wait_for_thread_event(stream.closed)


@pytest.mark.asyncio
async def test_windows_reader_cancellation_never_uses_default_executor(monkeypatch):
    """A stuck inherited stdin read is owned by a daemon, not asyncio.run's pool."""

    async def forbidden_to_thread(*args, **kwargs):
        raise AssertionError("Windows stdio reader must not use asyncio.to_thread")

    monkeypatch.setattr(service_module.asyncio, "to_thread", forbidden_to_thread)
    stream = _BlockingReadStream()
    reader = service_module._ThreadedStdioReader(stream)  # type: ignore[arg-type]
    read = asyncio.create_task(reader.readline())
    try:
        await _wait_for_thread_event(stream.started)
        assert reader._thread is not None and reader._thread.daemon

        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read

        reader.close()
        assert await reader.readline() == b""
    finally:
        stream.release.set()
        reader.close()
        await _wait_for_thread_event(stream.finished)


_DAEMON_READER_RUNTIME = r"""
import asyncio
import threading

from kestrel_sdk.isolated_feature.service import _ThreadedStdioReader


class BlockingStream:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def readline(self):
        self.started.set()
        self.release.wait()
        return b"unreachable\n"


async def forbidden_to_thread(*args, **kwargs):
    raise AssertionError("reader used the default executor")


async def main():
    asyncio.to_thread = forbidden_to_thread
    stream = BlockingStream()
    reader = _ThreadedStdioReader(stream)
    task = asyncio.create_task(reader.readline())
    while not stream.started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled readline unexpectedly completed")
    reader.close()


asyncio.run(main())
print("bounded-daemon-reader-exit")
"""


@pytest.mark.asyncio
async def test_windows_reader_cancelled_asyncio_run_exits_without_waiting_for_worker():
    """A blocked daemon reader cannot make asyncio.run wait for executor shutdown."""

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _DAEMON_READER_RUNTIME,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1)

    assert process.returncode == 0, stderr.decode()
    assert stdout in (
        b"bounded-daemon-reader-exit\n",
        b"bounded-daemon-reader-exit\r\n",
    )
    assert stderr == b""


_WRITER_SUCCESS_RELEASE_RUNTIME = r"""
import asyncio
import gc
import queue
import sys
import threading
import weakref

from kestrel_sdk.isolated_feature import service as service_module
from kestrel_sdk.isolated_feature.service import _ThreadedStdioWriter


BaseQueue = queue.Queue


class ObservedQueue(BaseQueue):
    def __init__(self):
        super().__init__()
        self.get_count = 0
        self.idle_get = threading.Event()

    def get(self, *args, **kwargs):
        self.get_count += 1
        if self.get_count == 2:
            self.idle_get.set()
        return super().get(*args, **kwargs)


service_module.queue.Queue = ObservedQueue


class PayloadOwner:
    def __init__(self):
        self.payload = b"successful frame bytes must not stay in the worker"


class OpenStream:
    def __init__(self):
        self.closed = threading.Event()

    def write(self, data):
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed.set()


async def wait_event(event):
    for _ in range(1_000):
        if event.is_set():
            return
        await asyncio.sleep(0)
    raise AssertionError("worker did not reach its expected state")


def writer_frame(writer):
    frame = sys._current_frames().get(writer._thread.ident)
    while frame is not None:
        if frame.f_code is _ThreadedStdioWriter._write_loop.__code__:
            return frame
        frame = frame.f_back
    raise AssertionError("writer worker frame was not found")


async def main():
    stream = OpenStream()
    stream_ref = weakref.ref(stream)
    writer = _ThreadedStdioWriter(stream)
    writer_ref = weakref.ref(writer)
    loop_ref = weakref.ref(asyncio.get_running_loop())
    owner = PayloadOwner()
    owner_ref = weakref.ref(owner)

    writer.write(owner.payload)
    drain = asyncio.create_task(writer.drain())
    for _ in range(1_000):
        with writer._state_lock:
            if writer._drains:
                completion = next(iter(writer._drains))
                break
        await asyncio.sleep(0)
    else:
        raise AssertionError("drain did not reach the writer")
    completion_ref = weakref.ref(completion)

    await drain
    await wait_event(writer._jobs.idle_get)
    owner = None
    completion = None
    drain = None
    for _ in range(10):
        gc.collect()

    assert owner_ref() is None
    assert completion_ref() is None
    assert not stream.closed.is_set()
    locals_ = writer_frame(writer).f_locals
    assert locals_["job"] is None
    assert locals_["data"] == b""
    assert locals_["loop"] is None
    assert locals_["completion"] is None

    writer.close()
    await wait_event(stream.closed)
    return loop_ref, owner_ref, completion_ref, writer_ref, stream_ref


loop_ref, owner_ref, completion_ref, writer_ref, stream_ref = asyncio.run(main())
for _ in range(10):
    gc.collect()

assert loop_ref() is None
assert owner_ref() is None
assert completion_ref() is None
assert writer_ref() is None
assert stream_ref() is None
print("writer-success-references-released")
"""


@pytest.mark.asyncio
async def test_windows_writer_success_releases_references_while_open_and_idle():
    """A successful idle writer must not pin its completed frame or drain."""

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _WRITER_SUCCESS_RELEASE_RUNTIME,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1)

    assert process.returncode == 0, stderr.decode()
    assert stdout in (
        b"writer-success-references-released\n",
        b"writer-success-references-released\r\n",
    )
    assert stderr == b""


_WRITER_TERMINAL_RELEASE_RUNTIME = r"""
import asyncio
import gc
import threading
import weakref

from kestrel_sdk.isolated_feature.service import _ThreadedStdioWriter


class PayloadOwner:
    def __init__(self):
        self.payload = b"queued payload owner must be released"


class FailingStream:
    def __init__(self):
        self.write_started = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()

    def write(self, data):
        self.write_started.set()
        self.release.wait()
        raise OSError("stream failure secret must not be retained")

    def flush(self):
        raise AssertionError("flush must not run after a write failure")

    def close(self):
        self.closed.set()


async def wait_event(event):
    for _ in range(1_000):
        if event.is_set():
            return
        await asyncio.sleep(0)
    raise AssertionError("worker did not reach its expected state")


async def main():
    stream = FailingStream()
    writer = _ThreadedStdioWriter(stream)
    loop_ref = weakref.ref(asyncio.get_running_loop())
    owner = PayloadOwner()
    owner_ref = weakref.ref(owner)

    writer.write(b"active frame")
    active = asyncio.create_task(writer.drain())
    await wait_event(stream.write_started)

    writer.write(owner.payload)
    queued = asyncio.create_task(writer.drain())
    for _ in range(1_000):
        with writer._state_lock:
            if len(writer._drains) == 2:
                break
        await asyncio.sleep(0)
    else:
        raise AssertionError("queued drain did not reach the writer")
    completion_refs = tuple(weakref.ref(item) for item in writer._drains)

    stream.release.set()
    for task in (active, queued):
        try:
            await task
        except ConnectionError:
            pass
        else:
            raise AssertionError("terminal drain unexpectedly completed")

    assert writer._jobs.empty()
    assert writer._pending == bytearray()
    assert not writer._drains
    await wait_event(stream.closed)
    return loop_ref, owner_ref, completion_refs


loop_ref, owner_ref, completion_refs = asyncio.run(main())
for _ in range(10):
    gc.collect()

assert loop_ref() is None
assert owner_ref() is None
assert all(reference() is None for reference in completion_refs)
print("writer-terminal-references-released")
"""


@pytest.mark.asyncio
async def test_windows_writer_terminal_failure_releases_queued_references():
    """A failed frame releases queued loops, completions, and payload owners."""

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _WRITER_TERMINAL_RELEASE_RUNTIME,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1)

    assert process.returncode == 0, stderr.decode()
    assert stdout in (
        b"writer-terminal-references-released\n",
        b"writer-terminal-references-released\r\n",
    )
    assert stderr == b""


_READER_BLOCKED_GC_RUNTIME = r"""
import asyncio
import gc
import threading
import weakref

from kestrel_sdk.isolated_feature.service import _ThreadedStdioReader


class BlockingStream:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def readline(self):
        self.started.set()
        self.release.wait()
        return b"unreachable\n"


async def main():
    stream = BlockingStream()
    stream_ref = weakref.ref(stream)
    reader = _ThreadedStdioReader(stream)
    reader_ref = weakref.ref(reader)
    loop_ref = weakref.ref(asyncio.get_running_loop())

    active = asyncio.create_task(reader.readline())
    while not stream.started.is_set():
        await asyncio.sleep(0)
    queued = asyncio.create_task(reader.readline())
    for _ in range(1_000):
        with reader._state_lock:
            if len(reader._pending) == 2:
                break
        await asyncio.sleep(0)
    else:
        raise AssertionError("queued read did not reach the reader")
    completion_refs = tuple(weakref.ref(item) for item in reader._pending)

    reader.close()
    assert not reader._pending
    assert reader._requests.qsize() == 1
    assert await active == b""
    assert await queued == b""
    return stream_ref, reader_ref, loop_ref, completion_refs


stream_ref, reader_ref, loop_ref, completion_refs = asyncio.run(main())
blocked_stream = stream_ref()
assert blocked_stream is not None
assert blocked_stream.started.is_set()
assert not blocked_stream.release.is_set()
for _ in range(10):
    gc.collect()

assert reader_ref() is None
assert loop_ref() is None
assert all(reference() is None for reference in completion_refs)
print("reader-blocked-references-released")
"""


@pytest.mark.asyncio
async def test_windows_reader_close_releases_references_while_read_stays_blocked():
    """A blocked daemon read must not pin its reader, futures, or closed loop."""

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _READER_BLOCKED_GC_RUNTIME,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1)

    assert process.returncode == 0, stderr.decode()
    assert stdout in (
        b"reader-blocked-references-released\n",
        b"reader-blocked-references-released\r\n",
    )
    assert stderr == b""


def test_private_wire_is_noninheritable():
    """The actual duplicated wire descriptor never leaks to child processes."""

    wire = service_module._open_private_wire()
    try:
        assert not os.get_inheritable(wire.fileno())
    finally:
        wire.close()


def test_private_wire_cleanup_when_noninheritability_setup_fails(monkeypatch):
    """A failed inheritable-bit update releases the duplicate immediately."""

    read_fd, duplicate_fd = os.pipe()

    def fail_set_inheritable(fd: int, inheritable: bool) -> None:
        assert fd == duplicate_fd
        assert inheritable is False
        raise OSError("cannot set inheritable bit")

    monkeypatch.setattr(service_module.os, "dup", lambda fd: duplicate_fd)
    monkeypatch.setattr(service_module.os, "set_inheritable", fail_set_inheritable)
    try:
        with pytest.raises(OSError, match="cannot set inheritable bit"):
            service_module._open_private_wire()
        with pytest.raises(OSError):
            os.fstat(duplicate_fd)
    finally:
        os.close(read_fd)
