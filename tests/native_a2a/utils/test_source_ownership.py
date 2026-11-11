"""Real backend handoffs; no Task, cancellation or completion mocks."""

import asyncio
import contextvars

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

import anyio
import pytest

from kestrel_sdk.native_a2a.utils.source_ownership import SourceHandoff
from anyio.abc import TaskGroup


class _Stoppable(Protocol):
    def request_stop(self) -> None: ...


@asynccontextmanager
async def _publishers(
    handoff: _Stoppable, *release_events: anyio.Event
) -> AsyncIterator[TaskGroup]:
    async with anyio.create_task_group() as group:
        try:
            yield group
        finally:
            handoff.request_stop()
            for event in release_events:
                event.set()


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_falsy_items_are_not_terminal_receipts(backend: str) -> None:
    async def run() -> None:
        handoff = SourceHandoff[object]()
        values = (None, '', 0)

        async def publish() -> None:
            for value in values:
                assert await handoff.publish(value)
            handoff.publish_terminal()

        with anyio.fail_after(5):
            async with _publishers(handoff) as group:
                group.start_soon(publish)
                for value in values:
                    assert await handoff.pull() == (True, value)
                assert await handoff.pull() == (False, None)

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_each_item_requires_its_own_ack(backend: str) -> None:
    async def run() -> None:
        handoff = SourceHandoff[int]()
        observed: list[int] = []

        async def publish() -> None:
            for value in (1, 2):
                assert await handoff.publish(value)
                observed.append(value)
            handoff.publish_terminal()

        with anyio.fail_after(5):
            async with _publishers(handoff) as group:
                group.start_soon(publish)
                assert await handoff.pull() == (True, 1)
                assert observed == []
                assert await handoff.pull() == (True, 2)
                assert observed == [1]
                assert await handoff.pull() == (False, None)
                assert observed == [1, 2]
        assert handoff.finished

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_stop_is_not_a_physical_retirement_ack(backend: str) -> None:
    async def run() -> None:
        handoff = SourceHandoff[int]()
        retiring, release = anyio.Event(), anyio.Event()

        async def publish() -> None:
            assert not await handoff.publish(1)
            retiring.set()
            await release.wait()
            handoff.publish_terminal()

        with anyio.fail_after(5):
            async with _publishers(handoff, release) as group:
                group.start_soon(publish)
                assert await handoff.pull() == (True, 1)
                assert handoff.item_outstanding
                handoff.request_stop()
                assert not handoff.item_outstanding
                await retiring.wait()
                assert not handoff.finished
                assert await handoff.pull() == (False, None)
                release.set()
        assert handoff.finished

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_wait_hooks_restore_original_context(backend: str) -> None:
    async def run() -> None:
        original = contextvars.ContextVar('original', default=False)
        phases: list[bool] = []

        @asynccontextmanager
        async def yield_writer() -> AsyncIterator[None]:
            token = original.set(False)
            try:
                yield
            finally:
                original.reset(token)

        handoff = SourceHandoff[int](yield_writer=yield_writer)

        async def publish() -> None:
            token = original.set(True)
            try:
                assert await handoff.publish(1)
                phases.append(original.get())
                handoff.publish_terminal()
            finally:
                original.reset(token)

        with anyio.fail_after(5):
            async with _publishers(handoff) as group:
                group.start_soon(publish)
                assert await handoff.pull() == (True, 1)
                assert await handoff.pull() == (False, None)
        assert phases == [True]
        assert original.get() is False

    anyio.run(run, backend=backend)


def test_direct_cancel_cannot_transfer_a_frame_ack_to_its_sibling() -> None:
    async def run() -> None:
        handoff = SourceHandoff[str]()
        errors: list[BaseException] = []
        delivered: list[bool] = []
        entered = asyncio.Event()

        async def first() -> None:
            try:
                await handoff.publish('A')
            except asyncio.CancelledError as error:
                errors.append(error)

        async def second() -> None:
            entered.set()
            delivered.append(await handoff.publish('B'))
            handoff.publish_terminal()

        with anyio.fail_after(5):
            async with asyncio.TaskGroup() as group:
                try:
                    a = group.create_task(first())
                    assert await handoff.pull() == (True, 'A')
                    a.cancel('original A withdrawal')
                    b = group.create_task(second())
                    await entered.wait()
                    assert not a.done()
                    assert await handoff.pull() == (True, 'B')
                    await a
                    assert len(errors) == 1
                    assert errors[0].args == ('original A withdrawal',)
                    assert not b.done()
                    assert await handoff.pull() == (False, None)
                    await b
                    assert delivered == [True]
                finally:
                    handoff.request_stop()

    anyio.run(run, backend='asyncio')


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_cancelled_queued_emitter_does_not_publish(backend: str) -> None:
    async def run() -> None:
        handoff = SourceHandoff[str]()
        scopes: list[anyio.CancelScope] = []
        entered, stopped = anyio.Event(), anyio.Event()

        async def first() -> None:
            assert await handoff.publish('A')
            handoff.publish_terminal()

        async def second() -> None:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                entered.set()
                await handoff.publish('B')
            stopped.set()

        with anyio.fail_after(5):
            async with _publishers(handoff) as group:
                group.start_soon(first)
                assert await handoff.pull() == (True, 'A')
                group.start_soon(second)
                await entered.wait()
                scopes[0].cancel()
                await anyio.wait_all_tasks_blocked()
                assert not stopped.is_set()
                assert await handoff.pull() == (False, None)
                await stopped.wait()
                assert scopes[0].cancelled_caught

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_stop_before_emission_never_enters_writer_hook(backend: str) -> None:
    async def run() -> None:
        phases: list[str] = []

        @asynccontextmanager
        async def writer() -> AsyncIterator[None]:
            phases.append('entered')
            yield

        handoff = SourceHandoff[int](yield_writer=writer)
        handoff.request_stop()
        assert not await handoff.publish(1)
        assert await handoff.pull() == (False, None)
        assert phases == []
        assert not handoff.finished

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_concurrent_reader_refuses_without_replacing_original(
    backend: str,
) -> None:
    async def run() -> None:
        entered = anyio.Event()

        @asynccontextmanager
        async def wait_writer() -> AsyncIterator[None]:
            entered.set()
            yield

        handoff = SourceHandoff[int](wait_writer=wait_writer)
        results: list[tuple[bool, int | None]] = []

        async def reader() -> None:
            results.append(await handoff.pull())

        with anyio.fail_after(5):
            async with _publishers(handoff) as group:
                group.start_soon(reader)
                await entered.wait()
                with pytest.raises(
                    RuntimeError, match='exactly one demand reader'
                ):
                    await handoff.pull()
                handoff.request_stop()
        assert results == [(False, None)]

    anyio.run(run, backend=backend)


def test_repeated_direct_withdrawal_retains_both_cancellations() -> None:
    async def run() -> None:
        handoff = SourceHandoff[int]()
        errors: list[BaseExceptionGroup] = []

        async def publisher() -> None:
            try:
                await handoff.publish(1)
            except BaseExceptionGroup as error:
                errors.append(error)

        with anyio.fail_after(5):
            async with asyncio.TaskGroup() as group:
                try:
                    task = group.create_task(publisher())
                    assert await handoff.pull() == (True, 1)
                    task.cancel('first withdrawal')
                    await anyio.wait_all_tasks_blocked()
                    task.cancel('second withdrawal')
                    await anyio.wait_all_tasks_blocked()
                    assert not task.done()
                    handoff.request_stop()
                    await task
                    assert task.cancelling() == 2
                finally:
                    handoff.request_stop()
        assert len(errors) == 1
        assert [error.args for error in errors[0].exceptions] == [
            ('first withdrawal',),
            ('second withdrawal',),
        ]

    anyio.run(run, backend='asyncio')


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
@pytest.mark.parametrize('queued', [1, 2])
def test_stop_reconciles_queued_frames_without_delivering_them(
    backend: str, queued: int
) -> None:
    async def run() -> None:
        handoff = SourceHandoff[int]()
        results: list[bool] = []

        async def publish(value: int) -> None:
            results.append(await handoff.publish(value))

        with anyio.fail_after(5):
            async with _publishers(handoff) as group:
                assert not handoff.stopped
                group.start_soon(publish, 1)
                await anyio.wait_all_tasks_blocked()
                assert handoff.waiting_for_ack
                if queued == 2:
                    group.start_soon(publish, 2)
                    await anyio.wait_all_tasks_blocked()
                handoff.request_stop()
                assert handoff.stopped
                assert await handoff.pull() == (False, None)
        assert results == [False] * queued
        assert not handoff.waiting_for_ack
        assert not handoff.finished

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_terminal_observation_refuses_a_new_publication(backend: str) -> None:
    async def run() -> None:
        handoff = SourceHandoff[int]()
        handoff.publish_terminal()
        with pytest.raises(RuntimeError, match='after terminal observation'):
            await handoff.publish(1)
        assert await handoff.pull() == (False, None)

    anyio.run(run, backend=backend)
