# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.
"""Real response execution, original OS failures and request-root retirement."""

from __future__ import annotations

import errno
import inspect
import os

from builtins import BaseExceptionGroup
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
import pytest

from kestrel_sdk.native_a2a.server.context import ServerCallContext
from kestrel_sdk.native_a2a.server.events import Event
from kestrel_sdk.native_a2a.server.request_handlers.request_handler import RequestHandler
from kestrel_sdk.native_a2a.server.routes import create_jsonrpc_routes, create_rest_routes
from kestrel_sdk.native_a2a.server.routes.common import create_event_source_response
from kestrel_sdk.native_a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from kestrel_sdk.native_a2a.server.routes.source_lifetime import (
    SourceRequest,
    SourceRequestRoot,
    create_source_route,
    source_request_response,
    source_root_for,
)
from kestrel_sdk.native_a2a.types.a2a_pb2 import (
    Message as AgentMessage,
    Part,
    Role,
    SendMessageRequest,
)
from kestrel_sdk.native_a2a.utils.source_lifetime import (
    SourceDemand,
    SourceFactory,
    standalone_source_lifetime,
)
from kestrel_sdk.native_a2a.utils.source_ownership import SourceAcquisition
from anyio.abc import TaskStatus
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tests.native_a2a.source_witness import (
    DescriptorSource,
    RootFailureProbe,
    StreamRequestHandler,
    assert_closed,
    original_owner,
)


def _scope() -> Scope:
    return {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': '2.4'},
        'http_version': '1.1',
        'method': 'POST',
        'scheme': 'http',
        'path': '/',
        'raw_path': b'/',
        'root_path': '',
        'query_string': b'',
        'headers': [],
        'server': ('testserver', 80),
        'client': ('testclient', 123),
    }


async def _receive() -> Message:
    await anyio.Event().wait()
    raise AssertionError('A completed response must retire its receive waiter')


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_root_probe_requires_an_actual_matching_original_failure(
    backend: str,
) -> None:
    async def run() -> None:
        original = LookupError('actual original ASGI failure')
        sent: list[Message] = []

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await Response('already sent')(scope, receive, send)
            raise original

        async def send(message: Message) -> None:
            sent.append(message)

        probe = RootFailureProbe(app)
        # No explicit expectation may turn a failed root into a normal return.
        with pytest.raises(LookupError) as uncaught:
            await probe(_scope(), _receive, send)
        assert uncaught.value is original
        assert probe.error is None
        with probe.expect(LookupError):
            await probe(_scope(), _receive, send)
        assert probe.error is probe.original is original
        assert sent[-1]['body'] == b'already sent'
        with pytest.raises(LookupError) as wrong, probe.expect(OSError):
            await probe(_scope(), _receive, send)
        assert wrong.value is original

        normal = RootFailureProbe(
            Response('a successful root is not expected failure')
        )
        with (
            pytest.raises(AssertionError, match='original ASGI root must fail'),
            normal.expect(LookupError),
        ):
            await normal(_scope(), _receive, send)

    anyio.run(run, backend=backend)


def _stream_application(
    handler: RequestHandler,
    frontend: str,
    binding: str,
    *,
    subscribe: bool = False,
) -> tuple[ASGIApp, str, dict, dict[str, str]]:
    compat = binding.endswith('03')
    headers = {'a2a-version': '0.3' if compat else '1.0'}
    if binding.startswith('rest'):
        routes = create_rest_routes(handler, enable_v0_3_compat=compat)
        route_groups = {'rest_routes': routes}
        prefix = (
            '/v1'
            if compat
            else '/tenant-one'
            if binding == 'rest-tenant'
            else ''
        )
        path = prefix + (
            '/tasks/task-one:subscribe' if subscribe else '/message:stream'
        )
        body = (
            {}
            if subscribe
            else {
                'message': {
                    'messageId': 'input',
                    'role': 'ROLE_USER',
                    'parts': [{'text': 'hi'}],
                }
            }
        )
    else:
        routes = create_jsonrpc_routes(handler, '/', enable_v0_3_compat=compat)
        route_groups = {'jsonrpc_routes': routes}
        path = '/'
        method = (
            ('tasks/resubscribe' if compat else 'SubscribeToTask')
            if subscribe
            else ('message/stream' if compat else 'SendStreamingMessage')
        )
        params = (
            {'id': 'task-one'}
            if subscribe
            else {
                'message': {
                    'messageId': 'input',
                    'role': 'user' if compat else 'ROLE_USER',
                    'parts': [{'text': 'hi'}],
                }
            }
        )
        body = {
            'jsonrpc': '2.0',
            'id': 'one',
            'method': method,
            'params': params,
        }
    if frontend == 'fastapi':
        app = FastAPI()
        add_a2a_routes_to_fastapi(app, **route_groups)
        # Internal policy injection must not become an HTTP query parameter.
        assert 'source_root' not in str(app.openapi())
        return app, path, body, headers
    return Starlette(routes=routes), path, body, headers


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_explicit_source_request_preserves_original_send_capability(
    backend: str,
) -> None:
    async def run() -> None:
        sent: list[Message] = []
        scope = _scope()
        scope['extensions'] = {'http.response.push': {}}
        scope['headers'] = [(b'accept', b'text/css')]

        async def send(message: Message) -> None:
            sent.append(message)

        original = Request(scope, receive=_receive, send=send)
        async with SourceRequestRoot(standalone_source_lifetime) as root:
            wrapped = SourceRequest(original, root)
            await wrapped.send_push_promise('/original.css')
        assert len(sent) == 1
        assert sent[0]['type'] == 'http.response.push'
        assert sent[0]['path'] == '/original.css'
        assert (b'accept', b'text/css') in sent[0]['headers']

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_explicit_source_request_preserves_original_cached_disconnect(
    backend: str,
) -> None:
    async def run() -> None:
        received = False

        async def receive() -> Message:
            nonlocal received
            assert not received, (
                'Cached disconnect must not consume the transport again'
            )
            received = True
            return {'type': 'http.disconnect'}

        original = Request(_scope(), receive=receive)
        assert await original.is_disconnected()
        async with SourceRequestRoot(standalone_source_lifetime) as root:
            assert await SourceRequest(original, root).is_disconnected()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_explicit_source_request_preserves_the_original_cached_body(
    backend: str,
) -> None:
    async def run() -> None:
        received = False
        encoded = b'{"message":"already parsed"}'

        async def receive() -> Message:
            nonlocal received
            if received:
                raise AssertionError(
                    'Cached body must not re-read the transport'
                )
            received = True
            return {'type': 'http.request', 'body': encoded, 'more_body': False}

        request = Request(_scope(), receive=receive)
        assert await request.json() == {'message': 'already parsed'}
        async with SourceRequestRoot(standalone_source_lifetime) as root:
            owned = SourceRequest(request, root)
            assert await owned.body() == encoded
            assert await owned.json() == {'message': 'already parsed'}
            assert [chunk async for chunk in owned.stream()] == [encoded, b'']

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_explicit_source_request_preserves_and_closes_original_cached_upload(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        upload = (tmp_path / 'original-upload').open('w+b')
        fd = upload.fileno()
        request = Request(_scope(), receive=_receive)
        # A pre-parsed ORIGINAL cache, not a claim about multipart decoding.
        form = FormData(
            {'upload': UploadFile(upload, filename='original-upload')}
        )
        request._form = form
        try:
            async with SourceRequestRoot(standalone_source_lifetime) as root:
                owned = SourceRequest(request, root)
                assert await owned.form() is form
                os.fstat(fd)
                await owned.close()
                assert upload.closed
                assert_closed(fd)
        finally:
            # Failure-only fixture cleanup cannot establish either assertion.
            upload.close()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_actual_sse_close_failure_retains_one_original_error_graph(
    tmp_path: Path, backend: str
) -> None:
    def leaves(error: BaseException) -> list[BaseException]:
        if isinstance(error, BaseExceptionGroup):
            return [
                leaf for child in error.exceptions for leaf in leaves(child)
            ]
        return [error]

    async def run() -> None:
        sources: list[DescriptorSource] = []

        def acquire(resources: SourceAcquisition) -> DescriptorSource:
            source = DescriptorSource(
                tmp_path / 'source', backend, (1,), fail_close=True
            )
            resources.retain(source, source.aclose)
            sources.append(source)
            return source

        async def endpoint(request: Request) -> Response:
            reader = await source_root_for(request).open(acquire)
            first = await anext(reader)

            async def events() -> AsyncIterator[dict[str, str]]:
                yield {'data': str(first)}
                async for value in reader:
                    yield {'data': str(value)}

            return create_event_source_response(events())

        async def send(message: Message) -> None:
            return None

        with (
            anyio.fail_after(5),
            pytest.raises((OSError, BaseExceptionGroup)) as caught,
        ):
            await source_request_response(endpoint)(_scope(), _receive, send)
        assert leaves(caught.value) == [sources[0].close_error]
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_request_root_spans_prefetch_and_real_sse_response(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        sources: list[DescriptorSource] = []
        sent: list[Message] = []

        def acquire(resources: SourceAcquisition) -> DescriptorSource:
            source = DescriptorSource(
                tmp_path / 'source', backend, (0, '', None)
            )
            resources.retain(source, source.aclose)
            sources.append(source)
            return source

        async def endpoint(request: Request) -> Response:
            source_root = source_root_for(request)
            assert request.scope['type'] == 'http'
            reader = await source_root.open(acquire)
            first = await anext(reader)
            assert not hasattr(reader, 'aclose')

            async def events() -> AsyncIterator[dict[str, str]]:
                yield {'data': repr(first)}
                async for value in reader:
                    yield {'data': repr(value)}

            return create_event_source_response(events())

        async def send(message: Message) -> None:
            sent.append(message)

        with anyio.fail_after(5):
            await source_request_response(endpoint)(_scope(), _receive, send)
        body = b''.join(message.get('body', b'') for message in sent)
        assert body == b"data: 0\r\n\r\ndata: ''\r\n\r\ndata: None\r\n\r\n"
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
@pytest.mark.parametrize('frontend', ['starlette', 'fastapi'])
@pytest.mark.parametrize(
    'binding', ['rest', 'rest-tenant', 'rest03', 'jsonrpc', 'jsonrpc03']
)
@pytest.mark.parametrize('subscribe', [False, True])
def test_real_dispatchers_preserve_original_source_through_all_frontends(
    tmp_path: Path, backend: str, frontend: str, binding: str, subscribe: bool
) -> None:
    async def run() -> None:
        sources: list[DescriptorSource[Event]] = []
        events = (
            AgentMessage(
                message_id='first',
                role=Role.ROLE_AGENT,
                parts=[Part(text='first')],
            ),
            AgentMessage(
                message_id='second',
                role=Role.ROLE_AGENT,
                parts=[Part(text='second')],
            ),
        )

        def acquire() -> DescriptorSource[Event]:
            source = DescriptorSource(tmp_path / 'source', backend, events)
            sources.append(source)
            return source

        handler = StreamRequestHandler(acquire)
        app, path, body, headers = _stream_application(
            handler, frontend, binding, subscribe=subscribe
        )
        with anyio.fail_after(5):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url='http://testserver',
            ) as client:
                response = await client.post(path, json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.headers['content-type'].startswith('text/event-stream')
        assert 'first' in response.text and 'second' in response.text
        assert len(sources) == 1
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
@pytest.mark.parametrize('frontend', ['starlette', 'fastapi'])
@pytest.mark.parametrize(
    'binding', ['rest', 'rest-tenant', 'rest03', 'jsonrpc', 'jsonrpc03']
)
@pytest.mark.parametrize('empty', [False, True])
def test_natural_generator_finally_failure_cannot_be_a_successful_http_root(
    tmp_path: Path, backend: str, frontend: str, binding: str, empty: bool
) -> None:
    async def run() -> None:
        sources: list[DescriptorSource[Event]] = []

        class Handler(StreamRequestHandler):
            async def on_message_send_stream(
                self,
                params: SendMessageRequest,
                context: ServerCallContext | None = None,
            ) -> AsyncIterator[Event]:
                source = DescriptorSource[Event](
                    tmp_path / 'generator', backend, (), fail_close=True
                )
                sources.append(source)
                try:
                    if not empty:
                        yield AgentMessage(
                            message_id='original', role=Role.ROLE_AGENT
                        )
                finally:
                    await source.aclose()

        def unused() -> DescriptorSource[Event]:
            pytest.fail(
                'The actual handler owns acquisition inside its generator'
            )

        app, path, body, headers = _stream_application(
            Handler(unused), frontend, binding
        )
        with anyio.fail_after(5), pytest.raises(BaseException) as root:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url='http://testserver',
            ) as client:
                await client.post(path, json=body, headers=headers)
        assert len(sources) == 1
        error = root.value
        while isinstance(error, BaseExceptionGroup):
            assert len(error.exceptions) == 1
            error = error.exceptions[0]
        assert error is sources[0].close_error
        assert error.errno == errno.EBADF
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_finished_http_response_cannot_outlive_a_blocked_original_close(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        closing, release = anyio.Event(), anyio.Event()
        sources: list[DescriptorSource] = []
        sent: list[Message] = []

        def acquire(resources: SourceAcquisition) -> DescriptorSource:
            source = DescriptorSource(
                tmp_path / 'source',
                backend,
                (1,),
                closing=closing,
                release=release,
            )
            resources.retain(source, source.aclose)
            sources.append(source)
            return source

        async def endpoint(request: Request) -> Response:
            source_root = source_root_for(request)
            reader = await source_root.open(acquire)
            return Response(str(await anext(reader)))

        async def send(message: Message) -> None:
            sent.append(message)

        async def request(
            *, task_status: TaskStatus[tuple[object, object]]
        ) -> None:
            task_status.started(original_owner(backend))
            await source_request_response(endpoint)(_scope(), _receive, send)

        with anyio.fail_after(5):
            async with anyio.create_task_group() as requests:
                task, coroutine = await requests.start(request)
                assert task is not None
                try:
                    await closing.wait()
                    assert sent[-1] == {
                        'type': 'http.response.body',
                        'body': b'1',
                    }
                    assert (
                        inspect.getcoroutinestate(coroutine)
                        != inspect.CORO_CLOSED
                    )
                    os.fstat(sources[0].fd)
                finally:
                    release.set()
            assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_early_normalizer_cannot_send_a_failed_original_retirement(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        sources: list[DescriptorSource] = []
        sent: list[Message] = []

        def acquire(resources: SourceAcquisition) -> DescriptorSource:
            source = DescriptorSource(
                tmp_path / 'source', backend, (), fail_close=True
            )
            resources.retain(source, source.aclose)
            sources.append(source)
            return source

        async def endpoint(request: Request) -> Response:
            source_root = source_root_for(request)
            reader = await source_root.open(acquire)
            with pytest.raises(OSError) as caught:
                await anext(reader)
            assert caught.value is sources[0].close_error
            return JSONResponse(
                {'error': 'ordinary protocol error'}, status_code=500
            )

        async def send(message: Message) -> None:
            sent.append(message)

        with anyio.fail_after(5), pytest.raises(OSError) as caught:
            await source_request_response(endpoint)(_scope(), _receive, send)
        assert caught.value is sources[0].close_error
        assert sent == []
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_real_send_failure_retires_the_original_before_propagating(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        sources: list[DescriptorSource] = []
        errors: list[OSError] = []
        fault, other_end = os.pipe()

        def acquire(resources: SourceAcquisition) -> DescriptorSource:
            source = DescriptorSource(tmp_path / 'source', backend, (1,))
            resources.retain(source, source.aclose)
            sources.append(source)
            return source

        async def endpoint(request: Request) -> Response:
            source_root = source_root_for(request)
            reader = await source_root.open(acquire)
            return Response(str(await anext(reader)))

        async def send(message: Message) -> None:
            if message['type'] == 'http.response.body':
                try:
                    os.write(fault, b'cannot write to a live pipe read end')
                except OSError as error:
                    errors.append(error)
                    raise

        try:
            with anyio.fail_after(5), pytest.raises(OSError) as caught:
                await source_request_response(endpoint)(
                    _scope(), _receive, send
                )
            assert errors == [caught.value]
            assert caught.value.errno == errno.EBADF
            sources[0].assert_original_retired()
        finally:
            os.close(fault)
            os.close(other_end)

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_explicit_policy_refusal_does_not_fall_back_to_standalone(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        acquired: list[SourceAcquisition] = []

        def acquire(resources: SourceAcquisition) -> DescriptorSource:
            acquired.append(resources)
            return DescriptorSource(tmp_path / 'must-not-acquire', backend, ())

        @asynccontextmanager
        async def refuse(
            factory: SourceFactory[object],
        ) -> AsyncIterator[SourceDemand[object]]:
            fd = os.open(
                tmp_path / 'missing-parent' / 'policy-refusal', os.O_RDWR
            )
            os.close(fd)
            pytest.fail(
                'Native refusal must happen before standalone admission'
            )
            yield  # pragma: no cover -- async context manager shape only

        with anyio.fail_after(5), pytest.raises(FileNotFoundError) as caught:
            async with SourceRequestRoot(refuse) as root:
                await root.open(acquire)
        assert caught.value.errno == errno.ENOENT
        assert acquired == []
        assert not (tmp_path / 'must-not-acquire').exists()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_source_root_requires_explicit_active_single_use(
    tmp_path: Path, backend: str
) -> None:
    async def run() -> None:
        sources: list[DescriptorSource] = []

        def acquire(resources: SourceAcquisition) -> DescriptorSource:
            source = DescriptorSource(tmp_path / 'source', backend, ())
            resources.retain(source, source.aclose)
            sources.append(source)
            return source

        root = SourceRequestRoot(standalone_source_lifetime)
        with pytest.raises(RuntimeError, match='active source root'):
            await root.open(acquire)
        assert sources == []
        async with root:
            reader = await root.open(acquire)
            with pytest.raises(StopAsyncIteration):
                await anext(reader)
            with pytest.raises(RuntimeError, match='already been admitted'):
                await root.open(acquire)
        with pytest.raises(RuntimeError, match='cannot be re-entered'):
            async with root:
                pytest.fail('A retired root cannot admit another source')
        sources[0].assert_original_retired()

        async def endpoint(request: Request) -> Response:
            return Response('metadata')

        route = create_source_route('/stream', endpoint, methods=['POST'])
        assert route.endpoint is endpoint
        assert route.path == '/stream'
        assert route.methods == {'POST'}

    anyio.run(run, backend=backend)
