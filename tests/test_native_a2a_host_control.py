"""Native HTTP routes must leave host control with its original caller (#117)."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager, nullcontext
from pathlib import Path

import httpx
import pytest
from google.protobuf.json_format import ParseError
from jsonrpc.exceptions import JSONRPCInvalidRequestException
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware.exceptions import ExceptionMiddleware

from kestrel_sdk.features import host_errors
from kestrel_sdk.native_a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from kestrel_sdk.native_a2a.server.routes.rest_routes import create_rest_routes
from kestrel_sdk.native_a2a.server.events.event_queue import EventQueueLegacy
from kestrel_sdk.native_a2a.types.a2a_pb2 import Task, TaskState, TaskStatus
from kestrel_sdk.native_a2a.utils._async_queue_compat import QueueShutDown
from kestrel_sdk.native_a2a.utils.errors import InternalError
from kestrel_sdk.native_a2a.utils.telemetry import trace_function

from tests.native_a2a.source_witness import RootFailureProbe


class HostControlError(Exception):
    """A dependency-free host's typed effect/retirement failure."""


class HostA2AControlError(HostControlError, InternalError):
    """An explicit host control failure that is also a normal A2A error type."""


class UnformattableHostControl(HostA2AControlError):
    """Formatting attempts a real unavailable resource, not a safe wire value."""

    def __init__(self, unavailable: Path) -> None:
        super().__init__("private host control must not be formatted")
        self.unavailable = unavailable
        self.formats = 0

    def __str__(self) -> str:
        self.formats += 1
        return self.unavailable.read_text()


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

    async def on_message_send(self, params, context):
        with self._original_invocation():
            raise self.original

    async def on_message_send_stream(self, params, context):
        with self._original_invocation():
            if self.phase == "after":
                yield Task(
                    id="original-task", context_id="original-context",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
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
    compatibility = binding == "jsonrpc-0.3"
    headers = {"a2a-version": "0.3" if compatibility else "1.0"}
    if binding in {"jsonrpc", "jsonrpc-0.3"}:
        method = "SubscribeToTask" if streaming else "GetTask"
        if compatibility:
            method = "tasks/resubscribe" if streaming else "tasks/get"
        return await client.post(
            "/rpc",
            json={
                "jsonrpc": "2.0", "id": "original-request",
                "method": method,
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
@pytest.mark.parametrize("error_type", [HostControlError, HostA2AControlError])
@pytest.mark.parametrize("compound", [False, True])
@pytest.mark.parametrize("phase", ["unary", "before", "after"])
@pytest.mark.parametrize("registered", [False, True])
async def test_enabled_compatibility_routes_keep_original_host_control(
    tmp_path, error_type, compound, phase, registered,
):
    control = error_type("private original compatibility control")
    cleanup = OSError("original compatibility cleanup debt")
    original = ExceptionGroup("original compatibility and cleanup", [control, cleanup]) if compound else control
    if registered:
        host_errors.register_control_error_predicate(_host_control)
    handler = RaisingHostHandler(original, tmp_path / "compatibility-effect", phase)
    app = Starlette(routes=create_jsonrpc_routes(handler, "/rpc", enable_v0_3_compat=True))
    root = RootFailureProbe(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=root, raise_app_exceptions=True),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        if registered:
            with pytest.raises(Exception) as captured:
                await _invoke(client, "jsonrpc-0.3", streaming=phase != "unary")
            assert _retains_original(captured.value, original)
        else:
            with root.expect(type(original)) if phase != "unary" else nullcontext():
                response = await _invoke(client, "jsonrpc-0.3", streaming=phase != "unary")
            if phase != "unary":
                assert root.original is original
            assert response.status_code == 200
            if phase == "unary":
                assert response.json()["error"]["code"] == -32603
            else:
                assert b'"error"' in response.content
    assert handler.calls == handler.retired == 1
    assert handler.witness.read_text() == "original handler effect"


class OriginalFailingRequestBody(httpx.AsyncByteStream):
    def __init__(self, error: Exception, witness: Path) -> None:
        self.error, self.witness = error, witness
        self.entered = self.retired = 0

    async def __aiter__(self):
        self.entered += 1
        try:
            self.witness.write_text("original request body effect")
            yield b"original request body bytes"
            raise self.error
        finally:
            self.retired += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["base", "params", "compat-params"])
@pytest.mark.parametrize("registered", [False, True])
async def test_request_validation_keeps_its_original_error_before_conversion(
    tmp_path, phase, registered,
):
    observed = []
    original_type = {
        "base": JSONRPCInvalidRequestException,
        "params": ParseError,
        "compat-params": ValidationError,
    }[phase]

    def classify(error):
        if isinstance(error, original_type):
            observed.append(error)
            return registered
        return False

    host_errors.register_control_error_predicate(classify)
    handler = RaisingHostHandler(HostControlError("handler must not run"), tmp_path / "forbidden-validation-handler")
    compatibility = phase == "compat-params"
    app = Starlette(routes=create_jsonrpc_routes(handler, "/rpc", enable_v0_3_compat=compatibility))
    body = {
        "jsonrpc": "2.0", "id": "original-validation",
        "method": "tasks/get" if compatibility else "GetTask",
        "params": "invalid base params" if phase == "base" else {"id": {"private invalid input": True}},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        async def invoke_original():
            return await client.post(
                "/rpc", json=body,
                headers={"a2a-version": "0.3" if compatibility else "1.0"},
            )
        if registered:
            with pytest.raises(Exception) as captured:
                await invoke_original()
            assert observed and captured.value is observed[0]
            assert all(error is captured.value for error in observed)
            assert captured.value.__traceback__ is not None
        else:
            response = await invoke_original()
            assert response.status_code == 200
            assert response.json()["error"]["code"] == (-32600 if phase == "base" else -32602)
    assert handler.calls == handler.retired == 0
    assert not handler.witness.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, OSError])
@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("compatibility", [False, True])
@pytest.mark.parametrize("public", [False, True])
async def test_streaming_request_body_failure_keeps_original_receive_control(
    tmp_path, error_type, registered, compatibility, public,
):
    original = error_type("private original receive evidence")
    if registered:
        host_errors.register_control_error_predicate(lambda error: error is original)
    body = OriginalFailingRequestBody(original, tmp_path / "request-body-effect")
    handler = RaisingHostHandler(HostControlError("handler must not run"), tmp_path / "forbidden-handler-effect")
    app = Starlette(routes=create_rest_routes(handler, enable_v0_3_compat=compatibility))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=not public),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        async def receive_original():
            return await client.request(
                "POST" if compatibility else "GET",
                "/v1/message:stream" if compatibility else "/tasks/original-task:subscribe",
                content=body, headers={"a2a-version": "0.3" if compatibility else "1.0"},
            )
        if registered and not public:
            with pytest.raises(error_type) as captured:
                await receive_original()
            assert captured.value is original
        else:
            response = await receive_original()
            assert response.status_code == (500 if registered else 400)
            if registered:
                assert b"private original receive evidence" not in response.content
            else:
                assert response.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert body.entered == body.retired == 1
    assert body.witness.read_text() == "original request body effect"
    assert handler.calls == handler.retired == 0
    assert not handler.witness.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("compatibility", [False, True])
async def test_actual_invalid_utf8_body_keeps_original_decoder_control(
    tmp_path, streaming, registered, public, compatibility,
):
    observed = []

    def classify(error):
        if isinstance(error, UnicodeDecodeError):
            observed.append(error)
            return registered
        return False

    host_errors.register_control_error_predicate(classify)
    handler = RaisingHostHandler(HostControlError("handler must not run"), tmp_path / "forbidden-utf8-handler")
    app = Starlette(routes=create_rest_routes(handler, enable_v0_3_compat=compatibility))
    payload = b"original-invalid-\xff"
    path = "/message:stream" if streaming else "/message:send"
    if compatibility:
        path = "/v1" + path
    root = RootFailureProbe(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=root, raise_app_exceptions=not public),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        async def decode_original():
            return await client.post(
                path, content=payload,
                headers={"a2a-version": "0.3" if compatibility else "1.0", "content-type": "application/json"},
            )
        if registered and not public:
            with pytest.raises(UnicodeDecodeError) as captured:
                await decode_original()
            assert observed and captured.value is observed[0]
            assert captured.value.object == payload
            assert captured.value.start == payload.index(b"\xff")
            frames = []
            traceback = captured.value.__traceback__
            while traceback is not None:
                frames.append(traceback.tb_frame.f_code.co_name)
                traceback = traceback.tb_next
            assert "parse_lenient" in frames
        else:
            with root.expect(ParseError) if streaming and not registered else nullcontext():
                response = await decode_original()
            if streaming and not registered:
                assert any(root.original.__cause__ is error for error in observed)
            assert response.status_code == (500 if registered else 400)
            if registered:
                assert observed and observed[0].object == payload
                assert b"original-invalid" not in response.content
                assert b"UnicodeDecodeError" not in response.content
            else:
                assert response.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert handler.calls == handler.retired == 0
    assert not handler.witness.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("public", [False, True])
async def test_legacy_rest_history_query_preserves_original_integer_parser(tmp_path, registered, public):
    observed = []

    def classify(error):
        if isinstance(error, ValueError):
            observed.append(error)
            return registered
        return False

    host_errors.register_control_error_predicate(classify)
    handler = RaisingHostHandler(HostControlError("handler must not run"), tmp_path / "forbidden-history-handler")
    app = Starlette(routes=create_rest_routes(handler, enable_v0_3_compat=True))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=not public),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        async def invoke():
            return await client.get("/v1/tasks/original-task?historyLength=private-invalid-integer", headers={"a2a-version": "0.3"})
        if registered and not public:
            with pytest.raises(ValueError) as captured:
                await invoke()
            assert observed and all(error is captured.value for error in observed)
            assert captured.value.__traceback__ is not None
        else:
            response = await invoke()
            assert response.status_code == (500 if registered else 400)
            if registered:
                assert observed
                assert b"private-invalid-integer" not in response.content
            else:
                assert response.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert handler.calls == handler.retired == 0
    assert not handler.witness.exists()


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


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["jsonrpc", "jsonrpc-0.3", "rest"])
@pytest.mark.parametrize("phase", ["unary", "before", "after"])
async def test_registered_control_precedes_error_formatting(tmp_path, binding, phase):
    original = UnformattableHostControl(tmp_path / "unavailable-formatting-resource")
    host_errors.register_control_error_predicate(_host_control)
    handler = RaisingHostHandler(original, tmp_path / "formatting-effect", phase)
    app = Starlette(routes=[
        *create_jsonrpc_routes(handler, "/rpc", enable_v0_3_compat=True),
        *create_rest_routes(handler),
    ])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        with pytest.raises(Exception) as captured:
            await _invoke(client, binding, streaming=phase != "unary")
        assert _retains_original(captured.value, original)
    assert original.formats == 0
    assert handler.calls == handler.retired == 1
    assert handler.witness.read_text() == "original handler effect"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_trace_diagnostics_do_not_replace_original_control(tmp_path, asynchronous):
    original = UnformattableHostControl(tmp_path / "unavailable-trace-resource")
    witness = tmp_path / "trace-source-effect"
    forbidden = tmp_path / "forbidden-control-extractor"
    host_errors.register_control_error_predicate(_host_control)

    def extractor(span, args, kwargs, result, exception):
        forbidden.write_text("extractor ran after registered control")

    def operation():
        witness.write_text("original traced operation")
        raise original

    async def async_operation():
        operation()

    traced = trace_function(attribute_extractor=extractor)(async_operation if asynchronous else operation)
    with pytest.raises(UnformattableHostControl) as captured:
        if asynchronous:
            await traced()
        else:
            traced()
    assert captured.value is original
    assert original.formats == 0
    assert witness.read_text() == "original traced operation"
    assert not forbidden.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_trace_extractor_keeps_its_original_registered_resource_error(tmp_path, asynchronous):
    observed = []
    witness = tmp_path / "successful-trace-effect"
    missing = tmp_path / "unavailable-extractor-resource"

    def classify(error):
        if isinstance(error, FileNotFoundError) and error.filename == str(missing):
            observed.append(error)
            return True
        return False

    host_errors.register_control_error_predicate(classify)

    def extractor(span, args, kwargs, result, exception):
        missing.read_text()

    def operation():
        witness.write_text("successful original operation")

    async def async_operation():
        operation()

    traced = trace_function(attribute_extractor=extractor)(async_operation if asynchronous else operation)
    with pytest.raises(FileNotFoundError) as captured:
        if asynchronous:
            await traced()
        else:
            traced()
    assert observed and all(error is captured.value for error in observed)
    assert captured.value.filename == str(missing)
    assert witness.read_text() == "successful original operation"


@pytest.fixture
def original_trace_exporter(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from kestrel_sdk.native_a2a.utils import telemetry

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Inject an actual OTel provider, not a mock span/recorder or global provider.
    monkeypatch.setattr(telemetry, "trace", provider)
    try:
        yield exporter
    finally:
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [False, True])
async def test_real_queue_shutdown_is_classified_before_telemetry(tmp_path, registered, original_trace_exporter):
    observed = []
    extractor_witness = tmp_path / "queue-extractor"

    def classify(error):
        if isinstance(error, QueueShutDown):
            observed.append(error)
            return registered
        return False

    def extractor(span, args, kwargs, result, exception):
        extractor_witness.write_text("ordinary queue extraction")

    host_errors.register_control_error_predicate(classify)
    queue = EventQueueLegacy()
    await queue.close()
    assert queue.is_closed() and queue.queue.empty()
    traced = trace_function(attribute_extractor=extractor)(queue.dequeue_event)
    with pytest.raises(QueueShutDown) as captured:
        await traced()
    spans = original_trace_exporter.get_finished_spans()
    assert spans
    if registered:
        assert observed and all(error is captured.value for error in observed)
        assert all(not span.events for span in spans)
        assert not extractor_witness.exists()
    else:
        assert any(span.events for span in spans)
        assert extractor_witness.read_text() == "ordinary queue extraction"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("registered", [False, True])
async def test_trace_compound_cancellation_preserves_original_group(tmp_path, asynchronous, registered, original_trace_exporter):
    original = BaseExceptionGroup("original control and cancellation", [HostControlError("original host failure"), asyncio.CancelledError()])
    witness = tmp_path / "compound-source"
    extractor_witness = tmp_path / "compound-extractor"
    missing = tmp_path / "unavailable-compound-extractor"
    host_errors.register_control_error_predicate(lambda error: registered and (_host_control(error) or isinstance(error, FileNotFoundError)))

    def extractor(span, args, kwargs, result, exception):
        extractor_witness.write_text("ordinary compound extraction")
        missing.read_text()

    def operation():
        witness.write_text("original compound source")
        raise original

    async def async_operation():
        operation()

    traced = trace_function(attribute_extractor=extractor)(async_operation if asynchronous else operation)
    with pytest.raises(BaseExceptionGroup) as captured:
        if asynchronous:
            await traced()
        else:
            traced()
    assert captured.value is original
    assert witness.read_text() == "original compound source"
    assert extractor_witness.exists() is not registered
    assert all(not span.events for span in original_trace_exporter.get_finished_spans())


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_trace_keeps_bare_cancellation_out_of_host_classification(tmp_path, asynchronous):
    original = asyncio.CancelledError()
    observed = []
    witness = tmp_path / "bare-cancel-extractor"
    host_errors.register_control_error_predicate(lambda error: observed.append(error) or False)

    def extractor(span, args, kwargs, result, exception):
        assert exception is None
        witness.write_text("ordinary cancellation diagnostic")

    def operation():
        raise original

    async def async_operation():
        operation()

    traced = trace_function(attribute_extractor=extractor)(async_operation if asynchronous else operation)
    with pytest.raises(asyncio.CancelledError) as captured:
        if asynchronous:
            await traced()
        else:
            traced()
    assert captured.value is original
    assert not observed
    assert witness.read_text() == "ordinary cancellation diagnostic"


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

    root = RootFailureProbe(observed_app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=root, raise_app_exceptions=True),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        if registered:
            with pytest.raises(Exception) as captured:
                await _invoke(client, binding, streaming=True)
            assert _retains_original(captured.value, original)
            assert not any(b"event: error" in body for body in bodies)
        else:
            with root.expect(type(original)):
                response = await _invoke(client, binding, streaming=True)
            assert root.original is original
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
    root = RootFailureProbe(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=root, raise_app_exceptions=True),
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
            with root.expect(RuntimeError) if phase != "unary" else nullcontext():
                response = await _invoke(client, binding, streaming=phase != "unary")
            if phase != "unary":
                assert root.original.__cause__ is original
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
    root = RootFailureProbe(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=root, raise_app_exceptions=True),
        base_url="http://native-host.invalid", trust_env=False,
    ) as client:
        if registered:
            with pytest.raises(Exception) as captured:
                await _invoke(client, binding, streaming=phase != "unary")
            assert _retains_original(captured.value, original)
        else:
            with root.expect(type(original)) if phase != "unary" else nullcontext():
                response = await _invoke(client, binding, streaming=phase != "unary")
            if phase != "unary":
                assert root.original is original
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
