# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.
"""Explicit idle withdrawal preserves the actual generator and its finally."""

import os

from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path

import anyio
import pytest

from kestrel_sdk.native_a2a.utils.source_lifetime import standalone_source_lifetime
from kestrel_sdk.native_a2a.utils.source_ownership import (
    SourceAcquisition,
    SourceHandoff,
    SourceWithdrawal,
    WakeableSource,
)

from tests.native_a2a.source_witness import DescriptorSource


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
@pytest.mark.parametrize('mapped', [False, True])
@pytest.mark.parametrize('fail_close', [False, True])
def test_idle_withdrawal_joins_original_finally_not_only_demand(
    tmp_path: Path, backend: str, mapped: bool, fail_close: bool
) -> None:
    async def run() -> None:
        withdrawal = SourceWithdrawal()
        idle, closing, release = anyio.Event(), anyio.Event(), anyio.Event()
        handles: list[DescriptorSource[int]] = []
        generators: list[AsyncGenerator[int, None]] = []
        errors: list[OSError] = []

        async def original() -> AsyncGenerator[int, None]:
            handle = DescriptorSource[int](
                tmp_path / 'idle-source',
                backend,
                (),
                closing=closing,
                release=release,
                fail_close=fail_close,
            )
            handles.append(handle)
            try:
                yield 1
                idle.set()
                await withdrawal.wait()
            finally:
                await handle.aclose()

        def acquire(resources: SourceAcquisition) -> AsyncIterator[int]:
            source = original()
            generators.append(source)
            wakeable = WakeableSource(source, withdrawal)
            if not mapped:
                return wakeable
            # This is the same acquisition shape as the actual dispatchers:
            # retain the handler's original before exposing a mapping source.
            iterator = resources.retain_iterable(wakeable)
            assert resources.retain_iterable(wakeable) is iterator

            async def mapped_source() -> AsyncIterator[int]:
                async for value in iterator:
                    yield value

            return mapped_source()

        try:
            with anyio.fail_after(5):
                async with standalone_source_lifetime(acquire) as reader:
                    assert await anext(reader) == 1
                    demand_ended = anyio.Event()

                    async def next_demand() -> None:
                        with pytest.raises(StopAsyncIteration):
                            await anext(reader)
                        demand_ended.set()

                    async with anyio.create_task_group() as consumers:
                        consumers.start_soon(next_demand)
                        try:
                            await idle.wait()
                            assert not withdrawal.requested
                            reader.request_stop()
                            reader.request_stop()
                            await closing.wait()
                            await demand_ended.wait()
                            assert withdrawal.requested
                            assert generators[0].ag_frame is not None
                            # Demand ended, but the REAL original descriptor
                            # remains open in its blocked original finally.
                            os.fstat(handles[0].fd)
                            assert not handles[0].close_error
                        finally:
                            # Explicit test-only release permits a broken
                            # notification mutation to retire its SAME owner;
                            # this release is not successful product evidence.
                            withdrawal.request_stop()
                            release.set()
        except OSError as error:
            errors.append(error)
        assert len(handles) == 1
        assert generators[0].ag_frame is None
        handles[0].assert_original_retired()
        assert errors == ([handles[0].close_error] if fail_close else [])

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_withdrawal_before_registration_is_sticky_and_has_identity(
    backend: str,
) -> None:
    async def run() -> None:
        handoff = SourceHandoff[int]()
        withdrawal, unrelated = SourceWithdrawal(), SourceWithdrawal()
        resources = SourceAcquisition(handoff)
        entered: list[bool] = []

        async def original() -> AsyncGenerator[int, None]:
            entered.append(True)
            yield 1

        source = original()
        handoff.request_stop()
        assert (
            resources.retain_iterable(WakeableSource(source, withdrawal))
            is source
        )
        assert withdrawal.requested and not unrelated.requested
        assert not handoff.finished
        assert entered == []
        assert await resources.retire() == ()
        assert source.ag_frame is None
        with pytest.raises(RuntimeError, match='acquisition is closed'):
            resources.retain_iterable(WakeableSource(source, withdrawal))
        # A released registration is no longer a retained notification target.
        with pytest.raises(ValueError, match='not registered'):
            handoff.release_withdrawal(withdrawal)

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_wakeable_contract_rejects_arbitrary_signal_and_non_generator(
    backend: str,
) -> None:
    async def run() -> None:
        async def original() -> AsyncGenerator[int, None]:
            yield 1

        source = original()
        try:
            with pytest.raises(TypeError, match='SourceWithdrawal'):
                WakeableSource(source, object())  # type: ignore[arg-type]
            with pytest.raises(TypeError, match='async generator'):
                WakeableSource(object(), SourceWithdrawal())  # type: ignore[arg-type]
            handoff = SourceHandoff[int]()
            with pytest.raises(TypeError, match='SourceWithdrawal'):
                handoff.retain_withdrawal(object())  # type: ignore[arg-type]
        finally:
            await source.aclose()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_partial_acquisition_failure_retains_actual_original_before_registration(
    tmp_path: Path,
    backend: str,
) -> None:
    async def run() -> None:
        withdrawal = SourceWithdrawal()
        resources = SourceAcquisition()
        handles: list[DescriptorSource[int]] = []

        async def original() -> AsyncGenerator[int, None]:
            handle = DescriptorSource[int](tmp_path / 'partial', backend, ())
            handles.append(handle)
            try:
                yield 1
            finally:
                await handle.aclose()

        source = original()
        assert await anext(source) == 1
        with pytest.raises(RuntimeError, match='requires its source handoff'):
            resources.retain_iterable(WakeableSource(source, withdrawal))
        assert await resources.retire() == ()
        assert source.ag_frame is None
        assert not withdrawal.requested
        assert len(handles) == 1
        # Same direct Task/coroutine, no replacement producer/close owner.
        assert handles[0].phases[0][1] == handles[0].phases[-1][1]

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_direct_generator_delegation_preserves_send_throw_and_close(
    backend: str,
) -> None:
    async def run() -> None:
        entered: list[bool] = []
        failure = ValueError('direct throw')

        async def original() -> AsyncGenerator[int, None]:
            try:
                yield 1
                yield 2
            finally:
                entered.append(True)

        source = original()
        wrapper = WakeableSource(source, SourceWithdrawal())
        assert aiter(wrapper) is wrapper
        assert await anext(wrapper) == 1
        assert await wrapper.asend(None) == 2
        with pytest.raises(ValueError) as caught:
            await wrapper.athrow(failure)
        assert caught.value is failure
        await wrapper.aclose()
        assert source.ag_frame is None
        assert entered == [True]

    anyio.run(run, backend=backend)
