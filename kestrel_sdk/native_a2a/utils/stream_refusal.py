# Modified by Kestrel: isolated SDK namespace; see VENDORED.json.
"""Explicit logical stream outcomes, never source-retirement acknowledgements."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar, cast

from kestrel_sdk.features.host_errors import is_host_control_error

from kestrel_sdk.native_a2a.utils.errors import A2AError


if TYPE_CHECKING:
    from collections.abc import Callable

    from kestrel_sdk.native_a2a.utils.source_lifetime import SourceDemand


T = TypeVar('T')
R = TypeVar('R')


@dataclass(frozen=True, slots=True)
class StreamRefusal:
    """A terminal business-protocol result, not a failed source or close receipt.

    A handler emits this at its logical decision branch after its own resource
    lifetimes have settled. It must not convert an arbitrary body/finalizer
    exception into this value. Dispatchers still require actual source EOF and
    original retirement before projecting the enclosed ordinary protocol error.
    """

    error: A2AError

    def __post_init__(self) -> None:
        """Reject values that are not actual A2A protocol errors."""
        if not issubclass(type(self.error), A2AError):
            raise TypeError('A stream refusal requires an A2A protocol error')


def map_stream_item(
    item: T | StreamRefusal, mapper: Callable[[T], R]
) -> R | StreamRefusal:
    """Map only normal data; retain an explicit refusal without formatting it."""
    if issubclass(type(item), StreamRefusal):
        return cast('StreamRefusal', item)
    return mapper(cast('T', item))


async def drain_stream_refusal(
    stream: SourceDemand[object], refusal: StreamRefusal
) -> A2AError:
    """Return protocol data only after actual terminal-source EOF/join.

    The lifetime policy owns the original source and its close. This consumer
    performs no close, new Task, timeout or manufactured completion receipt.
    Late source/closer failures propagate instead of becoming protocol data.
    """
    try:
        await anext(stream)
    except StopAsyncIteration:
        pass
    else:
        stream.request_stop()
        raise ValueError(
            'A terminal stream refusal was followed by another item'
        )
    if is_host_control_error(refusal.error):
        raise refusal.error
    return refusal.error


class ProtocolStreamDemand(AsyncIterator[T]):
    """Borrow demand and project logical refusals only after original EOF/join.

    This is not a second source owner: it cannot close, admit or certify a
    producer. The enclosing request's explicitly selected lifetime policy
    still owns physical completion and every original failure.
    """

    def __init__(self, stream: SourceDemand[T | StreamRefusal]) -> None:
        """Retain the actual non-closing demand, not a copied source."""
        self._stream = stream
        self._terminal_refusal_seen = False

    def __aiter__(self) -> ProtocolStreamDemand[T]:
        """Return this borrowed protocol-demand view."""
        return self

    async def __anext__(self) -> T:
        """Read normal data or a physically settled logical protocol refusal."""
        item = await anext(self._stream)
        if issubclass(type(item), StreamRefusal):
            self._terminal_refusal_seen = True
            error = await drain_stream_refusal(
                self._stream, cast('StreamRefusal', item)
            )
            raise error
        return cast('T', item)

    def request_stop(self) -> None:
        """Withdraw demand without claiming original-source retirement."""
        self._stream.request_stop()

    def raise_if_retirement_failed(self) -> None:
        """Delegate physical failure evidence to the actual lifetime policy."""
        if self._terminal_refusal_seen:
            self._stream.raise_if_source_failed()
        self._stream.raise_if_retirement_failed()

    def raise_if_source_failed(self) -> None:
        """Delegate original source evidence, never an observer's cached error."""
        self._stream.raise_if_source_failed()
