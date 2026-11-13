# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.
"""Real original Tasks, file descriptors, context tokens and caller withdrawal."""

from __future__ import annotations

import asyncio
import errno
import inspect
import os

from builtins import BaseExceptionGroup, ExceptionGroup
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest

from kestrel_sdk.native_a2a.utils import source_lifetime
from kestrel_sdk.native_a2a.utils.source_lifetime import SourceReader, standalone_source_lifetime
from kestrel_sdk.native_a2a.utils.source_ownership import SourceAcquisition
from anyio.abc import TaskStatus

from tests.native_a2a.source_witness import (
    DescriptorSource as _DescriptorSource,
    assert_closed as _closed,
    original_owner as _owner,
)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_factory_iteration_and_retirement_use_one_original(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        sources: list[_DescriptorSource] = []

        def acquire(resources: SourceAcquisition) -> _DescriptorSource:
            source = _DescriptorSource(
                tmp_path / 'source', backend, (None, '', 0)
            )
            resources.retain(source, source.aclose)
            sources.append(source)
            return source

        with anyio.fail_after(5):
            async with standalone_source_lifetime(acquire) as reader:
                assert not hasattr(reader, 'aclose')
                assert [value async for value in reader] == [None, '', 0]
        assert len(sources) == 1
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_partial_factory_and_real_late_close_errors_retain_originals(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        sources: list[_DescriptorSource] = []
        original = RuntimeError('factory after retained acquisition')
        cause = ValueError('original cause')

        def acquire(resources: SourceAcquisition) -> _DescriptorSource:
            source = _DescriptorSource(
                tmp_path / 'source', backend, (), fail_close=True
            )
            resources.retain(source, source.aclose)
            sources.append(source)
            raise original from cause

        with anyio.fail_after(5), pytest.raises(ExceptionGroup) as root:
            async with standalone_source_lifetime(acquire) as reader:
                with pytest.raises(ExceptionGroup) as caught:
                    await anext(reader)
                assert caught.value.exceptions == (
                    original,
                    sources[0].close_error,
                )
                assert original.__cause__ is cause
                assert sources[0].close_error is not None
                assert sources[0].close_error.errno == errno.EBADF
        assert root.value is caught.value
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_delivered_body_error_does_not_acknowledge_a_successful_root(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        sources: list[_DescriptorSource] = []
        original = RuntimeError('body after retained acquisition')

        def acquire(resources: SourceAcquisition) -> _DescriptorSource:
            source = _DescriptorSource(tmp_path / 'source', backend, ())
            resources.retain(source, source.aclose)
            sources.append(source)
            raise original

        with anyio.fail_after(5), pytest.raises(RuntimeError) as root:
            async with standalone_source_lifetime(acquire) as reader:
                with pytest.raises(RuntimeError) as demand:
                    await anext(reader)
                assert demand.value is original
        assert root.value is original
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
@pytest.mark.parametrize('empty', [False, True])
def test_natural_generator_finally_error_is_fatal_after_demand_catches_it(
    tmp_path: Path, backend: str, empty: bool
) -> None:
    async def run() -> None:
        sources: list[_DescriptorSource] = []

        async def generate() -> AsyncIterator[str]:
            source = _DescriptorSource(
                tmp_path / 'generator', backend, (), fail_close=True
            )
            sources.append(source)
            try:
                if not empty:
                    yield 'original frame'
            finally:
                await source.aclose()

        with anyio.fail_after(5), pytest.raises(OSError) as root:
            async with standalone_source_lifetime(
                lambda _resources: generate()
            ) as reader:
                if not empty:
                    assert await anext(reader) == 'original frame'
                with pytest.raises(OSError) as delivered:
                    await anext(reader)
                assert delivered.value is sources[0].close_error
                assert delivered.value.errno == errno.EBADF
        assert root.value is delivered.value
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_reading_a_failed_close_does_not_acknowledge_successful_retirement(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        sources: list[_DescriptorSource] = []

        def acquire(resources: SourceAcquisition) -> _DescriptorSource:
            source = _DescriptorSource(
                tmp_path / 'source', backend, (), fail_close=True
            )
            resources.retain(source, source.aclose)
            sources.append(source)
            return source

        with anyio.fail_after(5), pytest.raises(OSError) as root:
            async with standalone_source_lifetime(acquire) as reader:
                with pytest.raises(OSError) as demand:
                    await anext(reader)
                assert demand.value is sources[0].close_error
        assert root.value is sources[0].close_error
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_original_error_already_retained_as_cause_is_not_duplicated(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        sources: list[_DescriptorSource] = []
        observer = RuntimeError('original transport failure with source cause')

        def acquire(resources: SourceAcquisition) -> _DescriptorSource:
            source = _DescriptorSource(
                tmp_path / 'source', backend, (), fail_close=True
            )
            sources.append(source)
            return source

        with anyio.fail_after(5), pytest.raises(RuntimeError) as root:
            async with standalone_source_lifetime(acquire) as reader:
                try:
                    await anext(reader)
                except OSError as original:
                    assert original is sources[0].close_error
                    raise observer from original
        assert root.value is observer
        assert observer.__cause__ is sources[0].close_error
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
@pytest.mark.parametrize('spoof', ['cause', 'group'])
def test_overridden_graph_properties_cannot_hide_original_close_failure(
    tmp_path: Path, backend: str, spoof: str
) -> None:
    async def run() -> None:
        sources: list[_DescriptorSource] = []

        class SpoofedCause(RuntimeError):
            @property
            def __cause__(self) -> BaseException | None:
                return sources[0].close_error

        class SpoofedGroup(ExceptionGroup):
            @property
            def exceptions(self) -> tuple[BaseException, ...]:
                error = sources[0].close_error
                assert error is not None
                return (error,)

        original = (
            SpoofedCause('original body failure')
            if spoof == 'cause'
            else SpoofedGroup(
                'original body group', [ValueError('actual body child')]
            )
        )

        def acquire(resources: SourceAcquisition) -> _DescriptorSource:
            source = _DescriptorSource(
                tmp_path / 'source', backend, (), fail_close=True
            )
            resources.retain(source, source.aclose)
            sources.append(source)
            raise original

        with anyio.fail_after(5), pytest.raises(BaseException) as root:
            async with standalone_source_lifetime(acquire) as reader:
                with pytest.raises(BaseException) as delivered:
                    await anext(reader)
        assert root.value is delivered.value
        assert type(root.value) is ExceptionGroup
        actual = BaseExceptionGroup.__dict__['exceptions'].__get__(root.value)
        assert len(actual) == 2
        assert actual[0] is original
        assert actual[1] is sources[0].close_error
        assert actual[1].errno == errno.EBADF
        assert BaseException.__dict__['__cause__'].__get__(original) is None
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_unobserved_late_close_failure_reaches_the_request_root(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        sources: list[_DescriptorSource] = []

        def acquire(resources: SourceAcquisition) -> _DescriptorSource:
            source = _DescriptorSource(
                tmp_path / 'source', backend, (1,), fail_close=True
            )
            resources.retain(source, source.aclose)
            sources.append(source)
            return source

        with anyio.fail_after(5), pytest.raises(OSError) as caught:
            async with standalone_source_lifetime(acquire) as reader:
                assert await anext(reader) == 1
        assert caught.value is sources[0].close_error
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


def test_repeated_raw_request_cancellation_does_not_abort_original_close(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        closing, release, received = anyio.Event(), anyio.Event(), anyio.Event()
        sources: list[_DescriptorSource] = []

        def acquire(resources: SourceAcquisition) -> _DescriptorSource:
            source = _DescriptorSource(
                tmp_path / 'source',
                'asyncio',
                (1,),
                closing=closing,
                release=release,
            )
            resources.retain(source, source.aclose)
            sources.append(source)
            return source

        async def request() -> None:
            async with standalone_source_lifetime(acquire) as reader:
                assert await anext(reader) == 1
                received.set()
                await anyio.Event().wait()

        with anyio.fail_after(5):
            original_request = asyncio.create_task(request())
            try:
                await received.wait()
                original_request.cancel('request withdrawal')
                await closing.wait()
                original_request.cancel('join withdrawal')
                await asyncio.sleep(0)
                assert not original_request.done()
                os.fstat(sources[0].fd)
            finally:
                release.set()
                with pytest.raises(BaseExceptionGroup) as caught:
                    await original_request
            assert [error.args for error in caught.value.exceptions] == [
                ('request withdrawal',),
                ('join withdrawal',),
            ]
        sources[0].assert_original_retired()

    anyio.run(run)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_stop_does_not_finish_the_request_while_original_close_is_blocked(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        closing, release = anyio.Event(), anyio.Event()
        sources: list[_DescriptorSource] = []

        def acquire(resources: SourceAcquisition) -> _DescriptorSource:
            source = _DescriptorSource(
                tmp_path / 'source',
                backend,
                (1,),
                closing=closing,
                release=release,
            )
            resources.retain(source, source.aclose)
            sources.append(source)
            return source

        async def request(
            *, task_status: TaskStatus[tuple[object, object]]
        ) -> None:
            task_status.started(_owner(backend))
            async with standalone_source_lifetime(acquire) as reader:
                assert await anext(reader) == 1

        with anyio.fail_after(5):
            async with anyio.create_task_group() as requests:
                original = await requests.start(request)
                try:
                    await closing.wait()
                    assert (
                        inspect.getcoroutinestate(original[1])
                        != inspect.CORO_CLOSED
                    )
                    os.fstat(sources[0].fd)
                finally:
                    release.set()
        assert inspect.getcoroutinestate(original[1]) == inspect.CORO_CLOSED
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_kernel_return_is_not_the_original_producers_physical_join(
    tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        epilogue, release, observing_join = (
            anyio.Event(),
            anyio.Event(),
            anyio.Event(),
        )
        sources: list[_DescriptorSource] = []
        readers: list[SourceReader[object]] = []
        original_run = source_lifetime._SourceRuntime.run
        original_observe = source_lifetime._SourceRuntime.observe_join

        async def held_original(
            self: source_lifetime._SourceRuntime[object],
            *,
            task_status: TaskStatus[None],
        ) -> None:
            await original_run(self, task_status=task_status)
            # This is the ACTUAL producer's remaining epilogue, after the real
            # kernel retired its FD and returned. The group must still join it.
            epilogue.set()
            await release.wait()

        async def observed_wait(
            self: source_lifetime._SourceRuntime[object],
        ) -> BaseException | None:
            observing_join.set()
            return await original_observe(self)

        monkeypatch.setattr(
            source_lifetime._SourceRuntime, 'run', held_original
        )
        monkeypatch.setattr(
            source_lifetime._SourceRuntime, 'observe_join', observed_wait
        )

        def acquire(resources: SourceAcquisition) -> _DescriptorSource:
            source = _DescriptorSource(tmp_path / 'source', backend, ())
            resources.retain(source, source.aclose)
            sources.append(source)
            return source

        async def request(
            *, task_status: TaskStatus[tuple[object, object]]
        ) -> None:
            task_status.started(_owner(backend))
            async with standalone_source_lifetime(acquire) as reader:
                readers.append(reader)
                with pytest.raises(StopAsyncIteration):
                    await anext(reader)

        with anyio.fail_after(5):
            async with anyio.create_task_group() as requests:
                original = await requests.start(request)
                try:
                    await epilogue.wait()
                    _closed(sources[0].fd)
                    assert (
                        inspect.getcoroutinestate(sources[0].phases[0][1][1])
                        != inspect.CORO_CLOSED
                    )
                    readers[0].request_stop()
                    await observing_join.wait()
                    assert (
                        inspect.getcoroutinestate(original[1])
                        != inspect.CORO_CLOSED
                    )
                finally:
                    release.set()
        assert inspect.getcoroutinestate(original[1]) == inspect.CORO_CLOSED
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_native_startup_failure_keeps_original_without_protocol_placeholder(
    tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        failures: list[OSError] = []
        originals: list[tuple[object, object]] = []

        async def failed_start(
            self: source_lifetime._SourceRuntime[object],
            *,
            task_status: TaskStatus[None],
        ) -> None:
            originals.append(_owner(backend))
            await anyio.lowlevel.checkpoint()
            try:
                fd = os.open(
                    tmp_path / 'missing-parent' / 'native-failure', os.O_RDWR
                )
            except OSError as error:
                failures.append(error)
                raise
            else:
                os.close(fd)
                pytest.fail('The absent parent must refuse native startup')

        monkeypatch.setattr(source_lifetime._SourceRuntime, 'run', failed_start)

        def acquire(resources: SourceAcquisition) -> _DescriptorSource:
            source = _DescriptorSource(
                tmp_path / 'must-not-acquire', backend, ()
            )
            resources.retain(source, source.aclose)
            return source

        def leaves(error: BaseException) -> list[BaseException]:
            if isinstance(error, BaseExceptionGroup):
                return [
                    leaf for child in error.exceptions for leaf in leaves(child)
                ]
            return [error]

        with anyio.fail_after(5), pytest.raises(BaseExceptionGroup) as caught:
            async with standalone_source_lifetime(acquire):
                pytest.fail('Failed original startup must not admit a reader')
        assert leaves(caught.value) == failures
        assert len(failures) == 1 and failures[0].errno == errno.ENOENT
        assert not (tmp_path / 'must-not-acquire').exists()
        assert len(originals) == 1
        assert inspect.getcoroutinestate(originals[0][1]) == inspect.CORO_CLOSED

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_scope_withdrawal_does_not_cut_off_the_original_task_finally(
    tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        retiring, release = anyio.Event(), anyio.Event()
        observing_join, request_finished = anyio.Event(), anyio.Event()
        sources: list[_DescriptorSource] = []
        tails: list[tuple[int, tuple[object, object]]] = []
        original_run = source_lifetime._SourceRuntime.run
        original_observe = source_lifetime._SourceRuntime.observe_join

        async def original_with_finally(
            self: source_lifetime._SourceRuntime[object],
            *,
            task_status: TaskStatus[None],
        ) -> None:
            try:
                await original_run(self, task_status=task_status)
            finally:
                if self.outcome is not None:
                    fd = os.open(
                        tmp_path / 'original-finally',
                        os.O_CREAT | os.O_EXCL | os.O_RDWR,
                        0o600,
                    )
                    tails.append((fd, _owner(backend)))
                    retiring.set()
                    await release.wait()
                    os.close(fd)

        async def observed_wait(
            self: source_lifetime._SourceRuntime[object],
        ) -> BaseException | None:
            observing_join.set()
            return await original_observe(self)

        monkeypatch.setattr(
            source_lifetime._SourceRuntime, 'run', original_with_finally
        )
        monkeypatch.setattr(
            source_lifetime._SourceRuntime, 'observe_join', observed_wait
        )

        def acquire(resources: SourceAcquisition) -> _DescriptorSource:
            source = _DescriptorSource(tmp_path / 'source', backend, ())
            resources.retain(source, source.aclose)
            sources.append(source)
            return source

        async def request(
            *,
            task_status: TaskStatus[
                tuple[anyio.CancelScope, tuple[object, object]]
            ],
        ) -> None:
            with anyio.CancelScope() as observer:
                task_status.started((observer, _owner(backend)))
                try:
                    async with standalone_source_lifetime(acquire) as reader:
                        with pytest.raises(StopAsyncIteration):
                            await anext(reader)
                finally:
                    request_finished.set()

        try:
            with anyio.fail_after(5):
                async with anyio.create_task_group() as requests:
                    observer, request_owner = await requests.start(request)
                    try:
                        await retiring.wait()
                        observer.cancel()
                        while not (
                            observing_join.is_set() or request_finished.is_set()
                        ):
                            await anyio.lowlevel.checkpoint()
                        assert (
                            inspect.getcoroutinestate(request_owner[1])
                            != inspect.CORO_CLOSED
                        )
                        assert (
                            inspect.getcoroutinestate(tails[0][1][1])
                            != inspect.CORO_CLOSED
                        )
                        os.fstat(tails[0][0])
                    finally:
                        release.set()
            _closed(tails[0][0])
            sources[0].assert_original_retired()
        finally:
            # Failure-only fixture cleanup is AFTER physical-join/FD assertions.
            for fd, _owner_record in tails:
                try:
                    os.close(fd)
                except OSError as error:
                    if error.errno != errno.EBADF:
                        raise

    anyio.run(run, backend=backend)
