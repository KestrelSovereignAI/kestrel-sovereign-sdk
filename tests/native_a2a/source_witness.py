# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.
"""Shared real original-Task/FD/context witnesses for source lifetime tests."""

from __future__ import annotations

import asyncio
import contextvars
import errno
import inspect
import os

from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, TypeVar

import anyio
import pytest

from kestrel_sdk.native_a2a.server.context import ServerCallContext
from kestrel_sdk.native_a2a.server.events import Event
from kestrel_sdk.native_a2a.server.request_handlers.request_handler import RequestHandler
from kestrel_sdk.native_a2a.server.routes.source_lifetime import SourceRequest, SourceRequestRoot
from kestrel_sdk.native_a2a.types.a2a_pb2 import SendMessageRequest, SubscribeToTaskRequest
from kestrel_sdk.native_a2a.utils.errors import UnsupportedOperationError
from kestrel_sdk.native_a2a.utils.source_lifetime import standalone_source_lifetime
from starlette.datastructures import Headers, QueryParams
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send


T = TypeVar('T')


class RootFailureProbe:
    """Observe an expected failed real root separately from HTTPX's buffer.

    HTTPX otherwise discards all buffered frames when an in-process ASGI app
    raises. This test-only host catches ONLY an explicitly expected original
    failure, and the expectation itself fails if the real root returns normally.
    It is not a runtime adapter, source owner or successful-retirement receipt.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.error: BaseException | None = None
        self.original: BaseException | None = None
        self._expected: type[BaseException] | None = None

    @contextmanager
    def expect(
        self, error_type: type[BaseException]
    ) -> Iterator[RootFailureProbe]:
        assert self._expected is None
        self.error = self.original = None
        self._expected = error_type
        try:
            yield self
            assert self.error is not None, 'The original ASGI root must fail'
        finally:
            self._expected = None

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        try:
            await self.app(scope, receive, send)
        except BaseException as error:
            if self._expected is None:
                raise
            original = error
            while (
                isinstance(original, BaseExceptionGroup)
                and len(original.exceptions) == 1
            ):
                original = original.exceptions[0]
            if type(original) is not self._expected:
                raise
            assert self.error is None, (
                'Expected exactly one original failed root'
            )
            self.error, self.original = error, original


def make_request(
    method: str = 'GET',
    path_params: dict | None = None,
    query_params: dict | None = None,
    headers: dict | None = None,
    body: bytes = b'{}',
) -> Request:
    request_headers = {'a2a-version': '1.0', **(headers or {})}
    scope = {
        'type': 'http',
        'method': method,
        'path': '/',
        'headers': Headers(request_headers).raw,
        'path_params': path_params or {},
        'query_string': str(QueryParams(query_params or {})).encode(),
    }
    received = False

    async def receive() -> Message:
        nonlocal received
        if received:
            await anyio.Event().wait()
            raise AssertionError('The request body has already been consumed')
        received = True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive=receive)


@asynccontextmanager
async def source_request(request: Request) -> AsyncIterator[SourceRequest]:
    async with SourceRequestRoot(standalone_source_lifetime) as root:
        yield SourceRequest(request, root)


def original_owner(backend: str) -> tuple[object, object]:
    if backend == 'asyncio':
        task = asyncio.current_task()
        assert task is not None
        return task, task.get_coro()
    import trio  # noqa: PLC0415 -- test-only backend

    task = trio.lowlevel.current_task()
    return task, task.coro


def assert_closed(fd: int) -> None:
    with pytest.raises(OSError) as caught:
        os.fstat(fd)
    assert caught.value.errno == errno.EBADF


class DescriptorSource(AsyncIterator[T]):
    def __init__(
        self,
        path: Path,
        backend: str,
        values: tuple[T, ...],
        *,
        closing: anyio.Event | None = None,
        release: anyio.Event | None = None,
        fail_close: bool = False,
    ) -> None:
        self.backend = backend
        self.values = iter(values)
        self.fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.write(self.fd, b'original source')
        self.context = contextvars.ContextVar('original source', default=False)
        self.token = self.context.set(True)
        self.phases: list[tuple[str, tuple[object, object]]] = []
        self.closing = closing
        self.release = release
        self.fail_close = fail_close
        self.close_error: OSError | None = None
        self._record('factory')

    def _record(self, phase: str) -> None:
        assert self.context.get() is True
        self.phases.append((phase, original_owner(self.backend)))

    def __aiter__(self) -> DescriptorSource[T]:
        self._record('iterator')
        return self

    async def __anext__(self) -> T:
        self._record('next')
        try:
            return next(self.values)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self._record('close')
        if self.closing is not None:
            self.closing.set()
        if self.release is not None:
            await self.release.wait()
        os.close(self.fd)
        self.context.reset(self.token)
        if self.fail_close:
            try:
                os.fsync(self.fd)
            except OSError as error:
                self.close_error = error
                raise

    def assert_original_retired(self) -> None:
        original = self.phases[0][1]
        assert all(
            task is original[0] and coroutine is original[1]
            for _phase, (task, coroutine) in self.phases
        )
        assert [phase for phase, _ in self.phases].count('close') == 1
        assert inspect.getcoroutinestate(original[1]) == inspect.CORO_CLOSED
        assert_closed(self.fd)


class StreamRequestHandler(RequestHandler):
    """A real stream-only implementation; every other operation is unsupported."""

    def __init__(self, factory: Callable[[], DescriptorSource[Event]]) -> None:
        self.factory = factory

    async def _unsupported(self, *args: Any, **kwargs: Any) -> Any:
        raise UnsupportedOperationError('This handler supports streaming only')

    on_get_task = _unsupported
    on_list_tasks = _unsupported
    on_cancel_task = _unsupported
    on_message_send = _unsupported
    on_create_task_push_notification_config = _unsupported
    on_get_task_push_notification_config = _unsupported
    on_list_task_push_notification_configs = _unsupported
    on_delete_task_push_notification_config = _unsupported
    on_get_extended_agent_card = _unsupported

    async def on_message_send_stream(
        self,
        params: SendMessageRequest | SubscribeToTaskRequest,
        context: ServerCallContext,
    ) -> AsyncGenerator[Event, None]:
        source = self.factory()
        try:
            async for event in source:
                yield event
        finally:
            await source.aclose()

    on_subscribe_to_task = on_message_send_stream
