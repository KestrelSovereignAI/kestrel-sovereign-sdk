# Modified by Kestrel: isolated SDK namespace; see VENDORED.json.
"""Standalone backend ownership for the shared original-source body.

The enclosing context belongs to the request's complete ASGI lifetime, not
only its endpoint call. A host with its own admission/authority policy supplies
its own adapter; this standalone AnyIO policy grants no host authority.
"""

from __future__ import annotations

from builtins import BaseExceptionGroup
from collections.abc import AsyncIterable, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar, cast

from kestrel_sdk.native_a2a.utils.source_ownership import (
    CallbackSource,
    SourceAcquisition,
    SourceBodyOutcome,
    SourceHandoff,
    run_source_body,
)


if TYPE_CHECKING:
    from anyio.abc import TaskStatus


T = TypeVar('T')
T_co = TypeVar('T_co', covariant=True)
SourceFactory = Callable[
    [SourceAcquisition], AsyncIterable[T] | CallbackSource[T]
]


class SourceDemand(Protocol[T_co]):
    """Borrowed demand only; the enclosing policy owns physical retirement."""

    def __aiter__(self) -> AsyncIterator[T_co]:
        """Return an asynchronous demand reader without a second close."""
        ...

    async def __anext__(self) -> T_co:
        """Read one frame or a physically joined original failure."""
        ...

    def request_stop(self) -> None:
        """Withdraw demand, without acknowledging physical retirement."""
        ...

    def raise_if_retirement_failed(self) -> None:
        """Raise known original retirement failures, never a body projection."""
        ...


class SourceLifetimeFactory(Protocol):
    """Explicit caller-selected admission and physical-retirement policy."""

    def __call__(
        self, acquire: SourceFactory[T]
    ) -> AbstractAsyncContextManager[SourceDemand[T]]:
        """Retain the actual source owner until the enclosing context exits."""
        ...


def _original_errors(errors: list[BaseException]) -> BaseException | None:
    """Retain original graphs without mutating their causes or tracebacks."""

    def contains_original(graph: BaseException, target: BaseException) -> bool:
        pending = [graph]
        visited: set[int] = set()
        while pending:
            current = pending.pop()
            if current is target:
                return True
            if id(current) in visited:
                continue
            visited.add(id(current))
            if isinstance(current, BaseExceptionGroup):
                pending.extend(current.exceptions)
            if current.__cause__ is not None:
                pending.append(current.__cause__)
        return False

    originals: list[BaseException] = []
    for error in errors:
        if not any(
            contains_original(original, error) for original in originals
        ):
            originals.append(error)
    if not originals:
        return None
    if len(originals) == 1:
        return originals[0]
    return BaseExceptionGroup('Original source lifetime failures', originals)


class _SourceRuntime(Generic[T]):
    def __init__(self, acquire: SourceFactory[T]) -> None:
        import anyio  # noqa: PLC0415 -- optional backend, not a base import

        if not callable(acquire):
            raise TypeError('An original source requires a callable factory')
        self.acquire = acquire
        self.handoff = SourceHandoff[T]()
        self.dispatch = anyio.Event()
        self.joined = anyio.Event()
        self.outcome: SourceBodyOutcome | None = None
        self.backend_errors: list[BaseException] = []
        self._terminal_error: BaseException | None = None

    async def run(self, *, task_status: TaskStatus[None]) -> None:
        """Enter the original Task before dispatching any factory code."""
        task_status.started()
        # Cancellation before admission cannot enter an unknown source finally:
        # the source and its shield do not exist on this side of the gate.
        await self.dispatch.wait()
        if not self.handoff.stopped:
            self.outcome = await run_source_body(self.acquire, self.handoff)

    async def monitor(self, *, task_status: TaskStatus[None]) -> None:
        """Publish terminal demand only after the ORIGINAL Task group joins."""
        import anyio  # noqa: PLC0415 -- optional backend

        announced = False
        try:
            with anyio.CancelScope(shield=True):
                async with anyio.create_task_group() as original:
                    await original.start(self.run)
                    task_status.started()
                    announced = True
                # The monitor owns the entire original Task, including any
                # epilogue after the shared body shield has exited. Observer
                # scope cancellation cannot replace that physical join.
        except BaseException as error:
            if not announced:
                # The start waiter needs the actual failure, not a normal
                # return that manufactures a missing-start announcement error.
                raise
            self.backend_errors.append(error)
        finally:
            self.handoff.publish_terminal()
            self.joined.set()

    def terminal_error(self) -> BaseException | None:
        """Read the outcome only after the original group's physical join."""
        if not self.joined.is_set():
            raise RuntimeError('The original source owner is not joined')
        if self._terminal_error is None:
            errors = list(self.backend_errors)
            if self.outcome is not None:
                errors.extend(
                    error
                    for error in (
                        self.outcome.body_error,
                        *self.outcome.retirement_errors,
                        *self.outcome.observation_errors,
                    )
                    if error is not None
                )
            self._terminal_error = _original_errors(errors)
        return self._terminal_error

    async def observe_join(self) -> BaseException | None:
        """Observe one wait; a withdrawal is separate from physical joining."""
        import anyio  # noqa: PLC0415 -- optional backend

        try:
            await self.joined.wait()
        except anyio.get_cancelled_exc_class() as error:
            return error
        return None

    def retirement_failed(self) -> bool:
        """Keep original retirement failures distinct from body delivery."""
        if not self.joined.is_set():
            raise RuntimeError('The original source owner is not joined')
        return bool(
            self.backend_errors
            or (
                self.outcome is not None
                and (
                    self.outcome.retirement_errors
                    or self.outcome.observation_errors
                )
            )
        )


class SourceReader(AsyncIterator[T]):
    """Non-owning demand view; it deliberately exposes no second aclose."""

    def __init__(self, runtime: _SourceRuntime[T]) -> None:
        self._runtime = runtime

    def __aiter__(self) -> SourceReader[T]:
        """Return the sole non-owning demand reader."""
        return self

    async def __anext__(self) -> T:
        """Acknowledge the prior frame or observe a joined original failure."""
        present, value = await self._runtime.handoff.pull()
        if present:
            return cast('T', value)
        if self._runtime.joined.is_set():
            error = self._runtime.terminal_error()
            if error is not None:
                raise error
        raise StopAsyncIteration

    def request_stop(self) -> None:
        """Withdraw demand without claiming that resources have retired."""
        self._runtime.handoff.request_stop()

    def raise_if_retirement_failed(self) -> None:
        """Prevent known close failures becoming ordinary protocol payloads.

        Pending retirement is not successful retirement. The context still
        owns and joins that pending work; this hook reports only facts already
        established by the actual original group's physical exit.
        """
        if self._runtime.joined.is_set() and self._runtime.retirement_failed():
            error = self._runtime.terminal_error()
            if error is None:
                raise RuntimeError('Failed retirement has no original error')
            raise error


@asynccontextmanager
async def standalone_source_lifetime(
    acquire: SourceFactory[T],
) -> AsyncIterator[SourceReader[T]]:
    """Admit one producer and join it before leaving the ASGI request root.

    Use this context around BOTH dispatch and response execution. The source
    factory, iterator operations and registered retirement run in the one
    original producer, using the shared body mechanism. A blocked original
    body/close keeps this context pending; caller cancellation is not an ACK.
    Reading or projecting an original error cannot acknowledge a successful
    lifetime: opaque iteration/callback code can contain its own finalizers,
    whose failures are not distinguished by a later successful aclose.
    """
    import anyio  # noqa: PLC0415 -- optional backend

    from anyio.lowlevel import checkpoint_if_cancelled  # noqa: PLC0415

    runtime = _SourceRuntime(acquire)
    observations: list[BaseException] = []
    admitted = False
    try:
        with anyio.CancelScope(shield=True) as retirement:
            async with anyio.create_task_group() as monitors:
                try:
                    await monitors.start(runtime.monitor)
                    admitted = True
                    runtime.dispatch.set()
                    retirement.shield = False
                    await checkpoint_if_cancelled()
                    yield SourceReader(runtime)
                except BaseException as error:  # noqa: BLE001 -- retained observer
                    observations.append(error)
                finally:
                    retirement.shield = True
                    runtime.handoff.request_stop()
                    runtime.dispatch.set()
                    # Wait for a monitor fact that follows the original group's
                    # actual join. Keep repeated raw request-Task withdrawals
                    # separate; never inject them into an active source finally.
                    if admitted:
                        while not runtime.joined.is_set():
                            observation = await runtime.observe_join()
                            if observation is not None:
                                observations.append(observation)
    except BaseException as error:  # noqa: BLE001 -- backend/scope exit is evidence
        observations.append(error)
    if runtime.joined.is_set():
        error = runtime.terminal_error()
        if error is not None:
            observations.append(error)
    error = _original_errors(observations)
    if error is not None:
        raise error
