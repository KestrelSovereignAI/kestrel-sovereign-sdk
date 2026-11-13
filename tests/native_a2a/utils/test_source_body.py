# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.
"""Original source acquisition/retirement using real Tasks, tokens and files."""

import errno
import os
import subprocess
import sys

from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from textwrap import dedent
from typing import IO, cast

import anyio
import pytest

from kestrel_sdk.native_a2a.utils.source_ownership import (
    CallbackSource,
    SourceAcquisition,
    SourceBodyOutcome,
    SourceHandoff,
    run_source_body,
)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_acquire_iterate_and_retire_in_the_original_task(
    backend: str, tmp_path: Path
) -> None:
    path = tmp_path / 'source.bin'
    path.write_bytes(b'x')

    async def run() -> None:
        current = ContextVar('original-source-context', default=False)
        phases: list[tuple[str, int]] = []
        files: list[IO[bytes]] = []
        descriptors: list[int] = []
        outcomes: list[SourceBodyOutcome] = []
        handoff = SourceHandoff[bytes]()
        returned = anyio.Event()

        def record(phase: str) -> None:
            assert current.get() is True
            phases.append((phase, anyio.get_current_task().id))

        class Iterator:
            def __aiter__(self) -> 'Iterator':
                record('aiter')
                return self

            async def __anext__(self) -> bytes:
                record('next')
                value = files[0].read(1)
                if not value:
                    raise StopAsyncIteration
                return value

            async def aclose(self) -> None:
                record('close_iterator')

        def acquire(resources: SourceAcquisition) -> AsyncIterator[bytes]:
            token = current.set(True)

            def restore() -> None:
                record('restore_token')
                current.reset(token)

            resources.retain(token, restore)
            record('factory')
            file = path.open('rb')
            files.append(file)
            descriptors.append(file.fileno())
            resources.retain(file, file.close)
            return Iterator()

        async def source() -> None:
            outcomes.append(await run_source_body(acquire, handoff))
            returned.set()

        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                try:
                    group.start_soon(source)
                    assert await handoff.pull() == (True, b'x')
                    assert os.fstat(descriptors[0]).st_size == 1
                    handoff.request_stop()
                    await returned.wait()
                    assert not handoff.finished
                finally:
                    handoff.request_stop()
        # The host has now joined the real source, not only its body result.
        handoff.publish_terminal()
        assert await handoff.pull() == (False, None)
        assert files[0].closed
        with pytest.raises(OSError) as closed:
            os.fstat(descriptors[0])
        assert closed.value.errno == errno.EBADF
        assert current.get() is False
        assert len({task for _, task in phases}) == 1
        assert [phase for phase, _ in phases] == [
            'factory',
            'aiter',
            'next',
            'close_iterator',
            'restore_token',
        ]
        assert outcomes == [SourceBodyOutcome()]

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
@pytest.mark.parametrize('failure_phase', ['factory', 'aiter', 'next', 'close'])
def test_partial_acquisition_failures_retain_originals_and_close_the_file(
    backend: str, failure_phase: str, tmp_path: Path
) -> None:
    path = tmp_path / 'partial.bin'
    path.write_bytes(b'partial')

    async def run() -> None:
        failure = RuntimeError(failure_phase)
        files: list[IO[bytes]] = []
        outcomes: list[SourceBodyOutcome] = []
        tasks: list[int] = []
        handoff = SourceHandoff[bytes]()

        class Iterator:
            def __aiter__(self) -> 'Iterator':
                tasks.append(anyio.get_current_task().id)
                if failure_phase == 'aiter':
                    raise failure
                return self

            async def __anext__(self) -> bytes:
                tasks.append(anyio.get_current_task().id)
                if failure_phase == 'next':
                    raise failure
                raise StopAsyncIteration

            async def aclose(self) -> None:
                tasks.append(anyio.get_current_task().id)
                if failure_phase == 'close':
                    raise failure

        def acquire(resources: SourceAcquisition) -> AsyncIterator[bytes]:
            tasks.append(anyio.get_current_task().id)
            file = path.open('rb')
            files.append(file)
            resources.retain(file, file.close)
            if failure_phase == 'factory':
                raise failure
            return Iterator()

        async def source() -> None:
            outcomes.append(await run_source_body(acquire, handoff))

        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(source)
        assert files[0].closed
        assert len(set(tasks)) == 1
        outcome = outcomes[0]
        if failure_phase == 'close':
            assert outcome.body_error is None
            assert outcome.retirement_errors == (failure,)
        else:
            assert outcome.body_error is failure
            assert outcome.retirement_errors == ()
        assert outcome.observation_errors == ()
        assert not handoff.finished  # Only the actual host publishes its join.

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_all_close_failures_and_policy_failure_remain_separate(
    backend: str,
) -> None:
    async def run() -> None:
        body, first, second, observation = (
            RuntimeError(name)
            for name in ('body', 'first', 'second', 'observer')
        )
        closed: list[str] = []

        def close_first() -> None:
            closed.append('first')
            raise first

        async def close_second() -> None:
            closed.append('second')
            await anyio.sleep(0)
            raise second

        def acquire(resources: SourceAcquisition) -> AsyncIterator[None]:
            resources.retain(object(), close_first)
            resources.retain(object(), close_second)
            raise body

        def on_body_error(error: BaseException) -> None:
            assert error is body
            raise observation

        outcome = await run_source_body(
            acquire, SourceHandoff[None](), on_body_error=on_body_error
        )
        assert outcome.body_error is body
        assert outcome.retirement_errors == (second, first)
        assert outcome.observation_errors == (observation,)
        assert closed == ['second', 'first']

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_callback_source_joins_its_structured_emitters(backend: str) -> None:
    async def run() -> None:
        handoff = SourceHandoff[int]()
        outcomes: list[SourceBodyOutcome] = []
        joined: list[int] = []
        finished = anyio.Event()

        async def producer(
            publish: Callable[[int], Awaitable[bool]],
        ) -> None:
            async def emit(value: int) -> None:
                await publish(value)
                joined.append(value)

            async with anyio.create_task_group() as group:
                group.start_soon(emit, 1)
                group.start_soon(emit, 2)

        def acquire(_resources: SourceAcquisition) -> CallbackSource[int]:
            return CallbackSource(producer)

        async def source() -> None:
            outcome = await run_source_body(acquire, handoff)
            outcomes.append(outcome)
            assert outcome.body_error is None
            finished.set()

        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                try:
                    group.start_soon(source)
                    observed = [await handoff.pull(), await handoff.pull()]
                    assert {
                        value for present, value in observed if present
                    } == {1, 2}
                    handoff.request_stop()
                    await finished.wait()
                    assert set(joined) == {1, 2}
                    assert outcomes == [SourceBodyOutcome()]
                finally:
                    handoff.request_stop()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_parent_scope_cancellation_does_not_abort_original_close(
    backend: str, tmp_path: Path
) -> None:
    path = tmp_path / 'retiring.bin'
    path.write_bytes(b'open')

    async def run() -> None:
        handoff = SourceHandoff[int]()
        closing, release, returned = (
            anyio.Event(),
            anyio.Event(),
            anyio.Event(),
        )
        files: list[IO[bytes]] = []
        descriptors: list[int] = []
        outcomes: list[SourceBodyOutcome] = []

        class Iterator:
            def __aiter__(self) -> 'Iterator':
                return self

            async def __anext__(self) -> int:
                return 1

            async def aclose(self) -> None:
                closing.set()
                await release.wait()
                assert os.fstat(descriptors[0]).st_size == 4

        def acquire(resources: SourceAcquisition) -> AsyncIterator[int]:
            file = path.open('rb')
            files.append(file)
            descriptors.append(file.fileno())
            resources.retain(file, file.close)
            return Iterator()

        async def source() -> None:
            outcomes.append(await run_source_body(acquire, handoff))
            returned.set()

        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                try:
                    group.start_soon(source)
                    assert await handoff.pull() == (True, 1)
                    handoff.request_stop()
                    await closing.wait()
                    group.cancel_scope.cancel()
                    with anyio.CancelScope(shield=True):
                        await anyio.wait_all_tasks_blocked()
                        assert not returned.is_set()
                        assert not files[0].closed
                        release.set()
                finally:
                    handoff.request_stop()
                    release.set()
        assert files[0].closed
        assert outcomes == [SourceBodyOutcome()]

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_acquisition_contract_refusals_and_one_close_attempt(
    backend: str,
) -> None:
    async def run() -> None:
        resources = SourceAcquisition()
        closed: list[object] = []
        original = object()

        def close() -> None:
            closed.append(original)

        assert resources.retain(original, close) is original
        with pytest.raises(ValueError, match='already registered'):
            resources.retain(original, close)
        with pytest.raises(TypeError, match='callable closer'):
            resources.retain(object(), cast(Callable, None))
        assert await resources.retire() == ()
        with pytest.raises(RuntimeError, match='acquisition is closed'):
            resources.retain(object(), close)
        with pytest.raises(RuntimeError, match='already ran'):
            await resources.retire()
        assert closed == [original]

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_invalid_callback_return_still_retires_acquisitions(
    backend: str,
) -> None:
    async def run() -> None:
        closed: list[bool] = []

        def close() -> None:
            closed.append(True)

        async def producer(_publish: Callable[[int], Awaitable[bool]]) -> None:
            return cast(None, True)

        def acquire(resources: SourceAcquisition) -> CallbackSource[int]:
            resources.retain(object(), close)
            return CallbackSource(producer)

        outcome = await run_source_body(acquire, SourceHandoff[int]())
        assert isinstance(outcome.body_error, TypeError)
        assert outcome.retirement_errors == ()
        assert closed == [True]
        with pytest.raises(TypeError, match='callable producer'):
            CallbackSource(cast(Callable, None))

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
@pytest.mark.parametrize('mode', ['invalid_aclose', 'invalid_close_result'])
def test_invalid_closing_contracts_fail_without_leaking_a_file(
    backend: str, mode: str, tmp_path: Path
) -> None:
    path = tmp_path / 'contract.bin'
    path.write_bytes(b'contract')

    async def run() -> None:
        files: list[IO[bytes]] = []

        class Empty:
            def __aiter__(self) -> 'Empty':
                return self

            async def __anext__(self) -> None:
                raise StopAsyncIteration

        class Invalid(Empty):
            aclose = 42

        def acquire(resources: SourceAcquisition) -> AsyncIterator[None]:
            file = path.open('rb')
            files.append(file)

            def close() -> None:
                file.close()
                if mode == 'invalid_close_result':
                    return cast(None, True)

            resources.retain(file, close)
            return Invalid() if mode == 'invalid_aclose' else Empty()

        outcome = await run_source_body(acquire, SourceHandoff[None]())
        assert files[0].closed
        if mode == 'invalid_aclose':
            assert isinstance(outcome.body_error, TypeError)
            assert outcome.retirement_errors == ()
        else:
            assert outcome.body_error is None
            assert len(outcome.retirement_errors) == 1
            assert isinstance(outcome.retirement_errors[0], TypeError)

    anyio.run(run, backend=backend)


def test_actual_backend_scope_exit_failure_does_not_erase_body_error() -> None:
    # A deliberately malformed provider scope runs in a separate real Task
    # and process. Its bad scope cannot contaminate pytest's own task stack.
    code = dedent("""
        import asyncio
        import anyio
        from kestrel_sdk.native_a2a.utils.source_ownership import run_source_body, SourceHandoff

        async def main():
            original = RuntimeError('original body failure')

            def acquire(resources):
                scope = anyio.CancelScope()
                def invalid_close():
                    scope.__enter__()
                resources.retain(scope, invalid_close)
                raise original

            task = asyncio.create_task(run_source_body(acquire, SourceHandoff()))
            result = await task
            assert task.done()
            assert result.body_error is original
            assert len(result.retirement_errors) == 1
            assert isinstance(result.retirement_errors[0], RuntimeError)
            assert 'cancel scope' in str(result.retirement_errors[0])
            assert result.observation_errors == ()
            print('ACTUAL_SCOPE_FAILURE_RETAINED')

        anyio.run(main, backend='asyncio')
    """)
    result = subprocess.run(
        [sys.executable, '-B', '-c', code],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert result.stdout.strip() == 'ACTUAL_SCOPE_FAILURE_RETAINED'
    assert 'Exception ignored' not in result.stderr
