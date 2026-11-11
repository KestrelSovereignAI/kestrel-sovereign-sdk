"""Native HTTP routes must leave host control with its original caller (#117)."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware.exceptions import ExceptionMiddleware

from kestrel_sdk.features import host_errors
from kestrel_sdk.native_a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from kestrel_sdk.native_a2a.server.routes.rest_routes import create_rest_routes
from kestrel_sdk.native_a2a.types.a2a_pb2 import Task, TaskState, TaskStatus
from kestrel_sdk.native_a2a.utils.errors import InternalError


class HostControlError(Exception):
    """A dependency-free host's typed effect/retirement failure."""


class HostA2AControlError(HostControlError, InternalError):
    """An explicit host control failure that is also a normal A2A error type."""


def _host_control(error: BaseException) -> bool:
    if isinstance(error, BaseExceptionGroup):
        return any(_host_control(child) for child in error.exceptions)
    return isinstance(error, HostControlError)


class RaisingHostHandler:
    """A real handler invocation with an observed effect before its failure."""

    def __init__(self, original: BaseException, witness: Path, phase: str = "before") -> None:
        self.original = original
        self.witness = witness
        self.phase = phase
        self.calls = 0
        self.retired = 0

    @contextmanager
    def _original_invocation(self):
        self.calls += 1
        try:
            self.witness.write_text("original handler effect")
            yield
        finally:
            self.retired += 1

    async def on_get_task(self, params, context):
        assert params.id == "original-task"
        with self._original_invocation():
            raise self.original

    async def on_subscribe_to_task(self, params, context):
        assert params.id == "original-task"
        with self._original_invocation():
            if self.phase == "after":
                yield Task(
                    id="original-task", context_id="original-context",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            raise self.original


@pytest.fixture(autouse=True)
def isolate_host_policy(monkeypatch):
    # Reset only the existing registration seam, not I/O or native handlers.
    monkeypatch.setattr(host_errors, "_control_error_predicate", None)


async def _invoke(client: httpx.AsyncClient, binding: str, *, streaming: bool = False):
    headers = {"a2a-version": "1.0"}
    if binding == "jsonrpc":
        return await client.post(
            "/rpc",
            json={
                "jsonrpc": "2.0", "id": "original-request",
                "method": "SubscribeToTask" if streaming else "GetTask",
                "params": {"id": "original-task"},
            },
            headers=headers,
        )
    path = "/tasks/original-task:subscribe" if streaming else "/tasks/original-task"
    return await client.get(path, headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["jsonrpc", "rest"])
@pytest.mark.parametrize("error_type", [HostControlError, HostA2AControlError])
@pytest.mark.parametrize("compound", [False, True])
@pytest.mark.parametrize("registered", [False, True])
async def test_native_routes_preserve_original_registered_host_failure(
    tmp_path, binding, error_type, compound, registered,
):
    control = error_type("original durable outcome remains host-owned")
    cleanup = OSError("original cleanup debt")
    original = ExceptionGroup("original effect and cleanup", [control, cleanup]) if compound else control
    if registered:
        host_errors.register_control_error_predicate(_host_control)
    handler = RaisingHostHandler(original, tmp_path / "original-effect")
    app = Starlette(routes=[
        *create_jsonrpc_routes(handler, "/rpc"),
        *create_rest_routes(handler),
    ])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        if registered:
            with pytest.raises(type(original)) as captured:
                await _invoke(client, binding)
            assert captured.value is original
            frames = []
            traceback = captured.value.__traceback__
            while traceback is not None:
                frames.append(traceback.tb_frame.f_code.co_name)
                traceback = traceback.tb_next
            assert "on_get_task" in frames
            if compound:
                assert captured.value.exceptions == (control, cleanup)
        else:
            response = await _invoke(client, binding)
            if binding == "jsonrpc":
                assert response.status_code == 200
                assert response.json()["error"]["code"] == -32603
            else:
                assert response.status_code == 500
                assert response.json()["error"]["status"] == "INTERNAL"
    assert handler.calls == handler.retired == 1
    assert handler.witness.read_text() == "original handler effect"


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["jsonrpc", "rest"])
async def test_registered_classifier_does_not_reclassify_ordinary_handler_error(
    tmp_path, binding,
):
    host_errors.register_control_error_predicate(_host_control)
    handler = RaisingHostHandler(ValueError("ordinary failure"), tmp_path / "ordinary-effect")
    app = Starlette(routes=[
        *create_jsonrpc_routes(handler, "/rpc"), *create_rest_routes(handler),
    ])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        response = await _invoke(client, binding)
    if binding == "jsonrpc":
        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32603
    else:
        assert response.status_code == 500
        assert response.json()["error"]["status"] == "INTERNAL"
    assert handler.calls == handler.retired == 1
    assert handler.witness.read_text() == "original handler effect"


def _retains_original(error: BaseException, original: BaseException) -> bool:
    if error is original:
        return True
    return isinstance(error, BaseExceptionGroup) and any(
        _retains_original(child, original) for child in error.exceptions
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["jsonrpc", "rest"])
@pytest.mark.parametrize("error_type", [HostControlError, HostA2AControlError])
@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("compound", [False, True])
@pytest.mark.parametrize("registered", [False, True])
async def test_stream_control_keeps_original_failure_before_or_after_first_event(
    tmp_path, binding, error_type, phase, compound, registered,
):
    control = error_type("original stream control")
    cleanup = OSError("original stream cleanup debt")
    original = ExceptionGroup("original stream and cleanup", [control, cleanup]) if compound else control
    if registered:
        host_errors.register_control_error_predicate(_host_control)
    handler = RaisingHostHandler(original, tmp_path / "stream-effect", phase)
    app = Starlette(routes=[
        *create_jsonrpc_routes(handler, "/rpc"), *create_rest_routes(handler),
    ])
    bodies = []

    async def observed_app(scope, receive, send):
        async def observe_send(message):
            if message["type"] == "http.response.body":
                bodies.append(message.get("body", b""))
            await send(message)
        await app(scope, receive, observe_send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observed_app, raise_app_exceptions=True),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        if registered:
            with pytest.raises(Exception) as captured:
                await _invoke(client, binding, streaming=True)
            assert _retains_original(captured.value, original)
            assert not any(b"event: error" in body for body in bodies)
        else:
            response = await _invoke(client, binding, streaming=True)
            if phase == "after":
                assert response.status_code == 200
                assert b"event: error" in response.content
            else:
                assert response.status_code == (200 if binding == "jsonrpc" else 500)
                assert "error" in response.json()
    assert any(b"original-task" in body for body in bodies) is (phase == "after")
    assert handler.calls == handler.retired == 1
    assert handler.witness.read_text() == "original handler effect"


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["jsonrpc", "rest"])
@pytest.mark.parametrize("phase", ["unary", "before", "after"])
@pytest.mark.parametrize("registered", [False, True])
async def test_typed_payload_failure_reaches_the_owning_host_boundary(
    tmp_path, binding, phase, registered,
):
    original = HTTPException(status_code=413, detail="private original payload evidence")
    if registered:
        host_errors.register_control_error_predicate(lambda error: error is original)
    handler = RaisingHostHandler(original, tmp_path / "original-payload-effect", phase)
    ordinary_http = ExceptionMiddleware(app=None)

    async def host_http_boundary(request, error):
        # The embedding owns its surrounding middleware too. Reuse Starlette's
        # actual ordinary HTTP projection rather than introduce a second codec.
        if host_errors.is_host_control_error(error):
            raise error
        return await ordinary_http.http_exception(request, error)

    app = Starlette(
        routes=[*create_jsonrpc_routes(handler, "/rpc"), *create_rest_routes(handler)],
        exception_handlers={HTTPException: host_http_boundary},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        if registered:
            with pytest.raises(Exception) as captured:
                await _invoke(client, binding, streaming=phase != "unary")
            if phase == "after":
                # Starlette cannot run an HTTP error handler after response
                # start: its native carrier retains the SAME original cause.
                assert type(captured.value) is RuntimeError
                assert captured.value.__cause__ is original
            else:
                assert captured.value is original
            frames = []
            traceback = original.__traceback__
            while traceback is not None:
                frames.append(traceback.tb_frame.f_code.co_name)
                traceback = traceback.tb_next
            assert ("on_get_task" if phase == "unary" else "on_subscribe_to_task") in frames
        else:
            response = await _invoke(client, binding, streaming=phase != "unary")
            if phase == "after":
                assert response.status_code == 200
                assert b"event: error" in response.content
            elif binding == "jsonrpc":
                assert response.status_code == 200
                assert response.json()["error"]["code"] == -32600
            else:
                assert response.status_code == 413
    assert handler.calls == handler.retired == 1
    assert handler.witness.read_text() == "original handler effect"


def _original_decode_error(kind: str) -> Exception:
    try:
        if kind == "json":
            json.loads('{"private original evidence":')
        else:
            b"private original evidence\xff".decode("utf-8")
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        return error
    pytest.fail("the original decoder unexpectedly accepted invalid input")


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["jsonrpc", "rest"])
@pytest.mark.parametrize("kind", ["json", "unicode"])
@pytest.mark.parametrize("phase", ["unary", "before", "after"])
@pytest.mark.parametrize("registered", [False, True])
async def test_original_decoder_control_precedes_typed_protocol_replacement(
    tmp_path, binding, kind, phase, registered,
):
    original = _original_decode_error(kind)
    if registered:
        host_errors.register_control_error_predicate(lambda error: error is original)
    handler = RaisingHostHandler(original, tmp_path / "original-decoder-effect", phase)
    app = Starlette(routes=[
        *create_jsonrpc_routes(handler, "/rpc"), *create_rest_routes(handler),
    ])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        if registered:
            with pytest.raises(Exception) as captured:
                await _invoke(client, binding, streaming=phase != "unary")
            assert _retains_original(captured.value, original)
        else:
            response = await _invoke(client, binding, streaming=phase != "unary")
            if phase == "after":
                assert response.status_code == 200
                assert b"event: error" in response.content
            elif binding == "jsonrpc":
                assert response.status_code == 200
                assert response.json()["error"]["code"] == -32700
            else:
                assert response.status_code == 500
                assert response.json()["error"]["status"] == "INTERNAL"
    assert handler.calls == handler.retired == 1
    assert handler.witness.read_text() == "original handler effect"


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["jsonrpc", "rest"])
@pytest.mark.parametrize("registered", [False, True])
async def test_native_unary_does_not_reclassify_bare_cancellation(
    tmp_path, binding, registered,
):
    observed = []

    def classify(error):
        observed.append(error)
        return _host_control(error)

    if registered:
        host_errors.register_control_error_predicate(classify)
    original = asyncio.CancelledError("original caller cancellation")
    handler = RaisingHostHandler(original, tmp_path / "cancelled-effect")
    app = Starlette(routes=[
        *create_jsonrpc_routes(handler, "/rpc"), *create_rest_routes(handler),
    ])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        with pytest.raises(asyncio.CancelledError) as captured:
            await _invoke(client, binding)
    assert captured.value is original
    assert not any(error is original for error in observed)
    assert handler.calls == handler.retired == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["jsonrpc", "rest"])
async def test_native_public_error_body_does_not_serialize_host_control(
    tmp_path, binding,
):
    host_errors.register_control_error_predicate(_host_control)
    original = HostControlError("private original control and cleanup evidence")
    handler = RaisingHostHandler(original, tmp_path / "public-boundary-effect")
    app = Starlette(routes=[
        *create_jsonrpc_routes(handler, "/rpc"), *create_rest_routes(handler),
    ])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        response = await _invoke(client, binding)
    assert response.status_code == 500
    assert str(original) not in response.text
    assert "HostControlError" not in response.text
    assert handler.calls == handler.retired == 1
