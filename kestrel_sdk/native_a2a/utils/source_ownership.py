# Modified by Kestrel: isolated SDK namespace; see VENDORED.json.
"""The one-item source handoff, independent of scheduling and host authority.

Derived from Kestrel's original-source iterator. A scheduler/host adapter owns
admission, the original source Task, physical retirement and terminal errors.
This mechanism never starts or cancels a Task. Requesting stop is NOT evidence
that the source or its resources have retired.
"""

from __future__ import annotations

import inspect

from builtins import BaseExceptionGroup
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar


T = TypeVar('T')
WaitContext = Callable[[], AbstractAsyncContextManager[None]]


@asynccontextmanager
async def _plain_wait() -> AsyncIterator[None]:
    yield


@dataclass
class _Frame(Generic[T]):
    value: T
    acknowledged: _Signal = field(repr=False)
    decision: bool | None = None

    def acknowledge(self, *, proceed: bool) -> None:
        if self.decision is None:
            self.decision = proceed
            self.acknowledged.set()


class _Signal(Protocol):
    def set(self) -> None: ...

    async def wait(self) -> None: ...


class SourceHandoff(Generic[T]):
    """Serialize callback emissions through their individual ACK/Stop frames.

    The consumer's next pull acknowledges its preceding frame. A cancelled
    callback publisher retains that exact handoff before releasing its mutex;
    another publisher cannot consume its ACK. Host writer release wraps the
    mutex, and restoration completes before publisher code resumes.

    ``pull()`` returning false only ends demand. The host must independently
    join its original owner; neither stop nor an empty handoff proves cleanup.
    """

    def __init__(
        self,
        *,
        yield_writer: WaitContext = _plain_wait,
        wait_writer: WaitContext = _plain_wait,
    ) -> None:
        import anyio  # noqa: PLC0415 -- optional HTTP backend, never a base import

        self._yield_writer = yield_writer
        self._wait_writer = wait_writer
        self._frames: deque[_Frame[T]] = deque()
        self._ready = anyio.Event()
        self._lock = anyio.Lock()
        self._publishing: _Frame[T] | None = None
        self._delivered: _Frame[T] | None = None
        self._stopped = False
        self._finished = False
        self._reading = False

    @property
    def stopped(self) -> bool:
        """Whether stop was requested, independently of physical retirement."""
        return self._stopped

    @property
    def finished(self) -> bool:
        """Whether the owner's adapter published its independent terminal fact."""
        return self._finished

    @property
    def item_outstanding(self) -> bool:
        """Whether demand has not yet acknowledged its delivered frame."""
        return self._delivered is not None and self._delivered.decision is None

    @property
    def waiting_for_ack(self) -> bool:
        """Whether an original publisher still owns its frame handoff."""
        return self._publishing is not None

    def request_stop(self) -> None:
        """Stop demand cooperatively without cancelling any source Task."""
        self._stopped = True
        if self._publishing is not None:
            self._publishing.acknowledge(proceed=False)
        self._ready.set()

    def publish_terminal(self) -> None:
        """Publish an owner observation; does not invent or join that owner."""
        self._finished = True
        self._ready.set()

    async def pull(self) -> tuple[bool, T | None]:
        """Acknowledge the previous frame and receive the next demand result."""
        import anyio  # noqa: PLC0415 -- optional HTTP backend

        if self._reading:
            raise RuntimeError(
                'An original source has exactly one demand reader'
            )
        self._reading = True
        try:
            if self._delivered is not None:
                self._delivered.acknowledge(proceed=True)
                self._delivered = None
            while not self._frames:
                if self._stopped or self._finished:
                    return False, None
                async with self._wait_writer():
                    await self._ready.wait()
            if self._stopped:
                return False, None
            frame = self._frames.popleft()
            if not self._frames:
                self._ready = anyio.Event()
            self._delivered = frame
            return True, frame.value
        finally:
            self._reading = False

    async def publish(self, value: T) -> bool:
        """Retain this exact frame through its own ACK/Stop and restoration."""
        import anyio  # noqa: PLC0415 -- optional HTTP backend

        from anyio.lowlevel import checkpoint_if_cancelled  # noqa: PLC0415

        if self._stopped:
            return False
        with anyio.CancelScope(shield=True) as scope:
            async with self._yield_writer():
                async with self._lock:
                    scope.shield = False
                    try:
                        await checkpoint_if_cancelled()
                    finally:
                        scope.shield = True
                    if self._stopped:
                        return False
                    if self._finished:
                        raise RuntimeError(
                            'Original source published after terminal observation'
                        )
                    frame = _Frame(value, anyio.Event())
                    self._publishing = frame
                    self._frames.append(frame)
                    self._ready.set()
                    cancellations: list[BaseException] = []
                    try:
                        while True:
                            try:
                                await frame.acknowledged.wait()
                                break
                            except anyio.get_cancelled_exc_class() as error:
                                cancellations.append(error)
                    finally:
                        self._publishing = None
                    if len(cancellations) == 1:
                        raise cancellations[0]
                    if cancellations:
                        raise BaseExceptionGroup(
                            'Original emission withdrawn', cancellations
                        )
        await checkpoint_if_cancelled()
        return not self._stopped


@dataclass(frozen=True)
class CallbackSource(Generic[T]):
    """A structured producer; false emission requests stop, never proves close.

    The producer must join its children before returning None. The admitted
    original owner runs the producer and retires its registered resources.
    """

    producer: Callable[[Callable[[T], Awaitable[bool]]], Awaitable[None]] = (
        field(repr=False)
    )

    def __post_init__(self) -> None:
        """Reject a non-callable producer without executing it."""
        if not callable(self.producer):
            raise TypeError('A callback source requires a callable producer')


CloseResource = Callable[[], Awaitable[None] | None]


def _require_none(result: object, operation: str) -> None:
    if result is not None:
        raise TypeError(f'An original {operation} must return None')


async def _retire_resource(close: CloseResource) -> BaseException | None:
    try:
        result = close()
        if inspect.isawaitable(result):
            result = await result
        _require_none(result, 'resource closer')
    except BaseException as error:  # noqa: BLE001 -- original failure, not success
        return error
    return None


class SourceAcquisition:
    """Retain partial acquisitions before the factory's next fallible step.

    Registrations have identity, not equality. This record supplies no Task
    admission or execution authority. An undisclosed resource remains the
    factory's exception-safety responsibility.
    """

    def __init__(self) -> None:
        self._resources: list[tuple[object, CloseResource]] = []
        self._closed = False

    def retain(self, resource: T, close: CloseResource) -> T:
        """Register exactly one original closer, then return its resource."""
        if self._closed:
            raise RuntimeError('Original source acquisition is closed')
        if not callable(close):
            raise TypeError('An acquired resource needs a callable closer')
        if any(original is resource for original, _ in self._resources):
            raise ValueError('An original resource is already registered')
        self._resources.append((resource, close))
        return resource

    def retain_iterator(self, iterator: object) -> None:
        """Retain an exposed iterator before its next acquisition/advancement."""
        if any(original is iterator for original, _ in self._resources):
            return
        close = getattr(iterator, 'aclose', None)
        if close is None:
            return  # AsyncIterable does not require an aclose capability.
        if not callable(close):
            raise TypeError('An exposed aclose capability must be callable')
        self.retain(iterator, close)

    def retain_iterable(self, source: AsyncIterable[T]) -> AsyncIterator[T]:
        """Retain source BEFORE aiter, then retain the actual iterator too."""
        self.retain_iterator(source)
        iterator = aiter(source)
        self.retain_iterator(iterator)
        return iterator

    async def retire(self) -> tuple[BaseException, ...]:
        """Attempt all original closers once, LIFO, retaining every failure."""
        if self._closed:
            raise RuntimeError('Original source retirement already ran')
        self._closed = True
        errors: list[BaseException] = []
        for _resource, close in reversed(self._resources):
            error = await _retire_resource(close)
            if error is not None:
                errors.append(error)
        return tuple(errors)


@dataclass(frozen=True)
class SourceBodyOutcome:
    """Body/retirement facts, separate from Task admission and reader withdrawal.

    Returning this value is NOT the host owner's physical Task-join receipt.
    Host epilogue and observation failures still belong to that actual owner.
    """

    body_error: BaseException | None = field(default=None, repr=False)
    retirement_errors: tuple[BaseException, ...] = field(default=(), repr=False)
    observation_errors: tuple[BaseException, ...] = field(
        default=(), repr=False
    )


async def run_source_body(
    acquire: Callable[
        [SourceAcquisition], AsyncIterable[T] | CallbackSource[T]
    ],
    handoff: SourceHandoff[T],
    *,
    on_body_error: Callable[[BaseException], None] | None = None,
) -> SourceBodyOutcome:
    """Run acquisition, demand and retirement in the admitted caller's Task.

    This is the shared body mechanism, NOT a scheduler or admission boundary.
    The host must invoke it behind its accepted owner's dispatch gate, retain
    that exact owner and publish terminal demand only after observing the owner.
    Neither this function nor a reader may cancel an unknown active finally.
    """
    import anyio  # noqa: PLC0415 -- optional HTTP backend

    resources = SourceAcquisition()
    body_error: BaseException | None = None
    observation_errors: list[BaseException] = []
    retirement_errors: tuple[BaseException, ...] = ()
    try:
        with anyio.CancelScope(shield=True):
            try:
                source = acquire(resources)
                if isinstance(source, CallbackSource):
                    result = await source.producer(handoff.publish)
                    _require_none(result, 'callback producer')
                else:
                    iterator = resources.retain_iterable(source)
                    while True:
                        try:
                            item = await anext(iterator)
                        except StopAsyncIteration:
                            break
                        if not await handoff.publish(item):
                            break
            except BaseException as error:  # noqa: BLE001 -- retain ALL original control
                body_error = error
                if on_body_error is not None:
                    try:
                        on_body_error(error)
                    except BaseException as observation_error:  # noqa: BLE001 -- independent evidence
                        observation_errors.append(observation_error)
            finally:
                retirement_errors = await resources.retire()
    except BaseException as scope_error:  # noqa: BLE001 -- scope retirement is not ACKed
        # Include failure of the original backend scope's exit itself. It is
        # neither successful cleanup nor the request observer's withdrawal.
        retirement_errors = (*retirement_errors, scope_error)
    return SourceBodyOutcome(
        body_error, retirement_errors, tuple(observation_errors)
    )
