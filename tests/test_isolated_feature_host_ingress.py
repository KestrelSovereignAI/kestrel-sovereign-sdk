"""Private, capability-negotiated host-ingress contract coverage."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from kestrel_sdk.isolated_feature import (
    HEALTH,
    HOST_INGRESS,
    HOST_INGRESS_CAPABILITY,
    HOST_INGRESS_VERSION,
    MAX_HOST_INGRESS_NAME_BYTES,
    MAX_HOST_INGRESS_PAYLOAD_BYTES,
    SHUTDOWN,
    ConfigTransitionResult,
    HostIngressCapabilities,
    HostIngressError,
    HostIngressUnknownNameError,
    HostIngressUnsupportedError,
    IsolatedFeatureClient,
    IsolatedFeatureService,
    JsonRpcRequest,
    ProtocolError,
    ToolMetadata,
    decode_message,
    encode_message,
    validate_host_ingress_name,
    validate_host_ingress_payload,
)

from .test_isolated_feature import memory_stdio_pair

_SCHEMA = {"type": "object", "properties": {}}


async def _ready_client(
    service: IsolatedFeatureService,
) -> tuple[IsolatedFeatureClient, asyncio.Task[None]]:
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)
    await client.initialize()
    await client.health()
    return client, service_task


async def _close_client(
    client: IsolatedFeatureClient, service_task: asyncio.Task[None]
) -> None:
    try:
        if not service_task.done():
            await client.shutdown()
        await service_task
    finally:
        await client.close()


def test_host_ingress_protocol_capability_and_boundary_validation() -> None:
    capabilities = HostIngressCapabilities(names=("config-sync", "host-event"))

    assert capabilities.version == HOST_INGRESS_VERSION
    assert capabilities.to_dict() == {
        "version": HOST_INGRESS_VERSION,
        "names": ["config-sync", "host-event"],
    }
    assert HostIngressCapabilities.from_dict(capabilities.to_dict()) == capabilities
    assert capabilities.supports("config-sync") is True
    assert capabilities.supports("missing") is False
    assert validate_host_ingress_name("a" * MAX_HOST_INGRESS_NAME_BYTES) == (
        "a" * MAX_HOST_INGRESS_NAME_BYTES
    )
    assert validate_host_ingress_payload({"nested": [None, True, 1, 1.5, "ok"]}) == {
        "nested": [None, True, 1, 1.5, "ok"]
    }

    for name in ("Host-event", "host_event", "host--event", "host-event-", "x" * 65):
        with pytest.raises(ProtocolError):
            validate_host_ingress_name(name)
    with pytest.raises(ProtocolError, match="requires version and names"):
        HostIngressCapabilities.from_dict(
            {"version": 1, "names": ["host-event"], "extra": True}
        )
    with pytest.raises(ProtocolError, match="valid JSON"):
        validate_host_ingress_payload({"not-json": object()})
    with pytest.raises(ProtocolError, match="size limit"):
        validate_host_ingress_payload("x" * MAX_HOST_INGRESS_PAYLOAD_BYTES)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ("host-\u00e9vent", "host-\ud800event"))
async def test_non_ascii_host_ingress_names_fail_closed(name: str) -> None:
    with pytest.raises(ProtocolError, match="lowercase slug"):
        validate_host_ingress_name(name)
    with pytest.raises(ProtocolError, match="lowercase slug"):
        HostIngressCapabilities.from_dict({"version": 1, "names": [name]})

    service = IsolatedFeatureService(name="fake", version="1.0.0")
    calls: list[object] = []
    service.register_host_ingress(
        "host-event", lambda payload: calls.append(payload) or {"ok": True}
    )
    client, service_task = await _ready_client(service)

    try:
        assert client.supports_host_ingress_name(name) is False
        with pytest.raises(HostIngressError, match="host ingress failed"):
            await client.call_host_ingress(name, {"secret": "host-only"})
        assert calls == []

        client.capabilities[HOST_INGRESS_CAPABILITY] = {
            "version": 1,
            "names": [name],
        }
        assert client.host_ingress_capabilities is None
        assert client.supports_host_ingress is False
        with pytest.raises(HostIngressUnsupportedError, match="not supported"):
            await client.call_host_ingress("host-event", {"secret": "host-only"})
        assert calls == []
    finally:
        await _close_client(client, service_task)


@pytest.mark.asyncio
async def test_registered_ingress_is_capability_negotiated_and_tool_invisible():
    service = IsolatedFeatureService(name="fake", version="1.0.0")
    observed: list[object] = []

    async def config_sync(payload):
        observed.append(payload)
        return {"accepted": True, "payload": payload}

    service.register_host_ingress("config-sync", config_sync)
    service.register_tool(
        ToolMetadata(
            name="agent-tool", description="Agent-visible tool", input_schema=_SCHEMA
        ),
        lambda arguments: {"ok": True},
    )
    client, service_task = await _ready_client(service)

    try:
        assert client.host_ingress_capabilities == HostIngressCapabilities(
            names=("config-sync",)
        )
        assert client.supports_host_ingress is True
        assert client.supports_host_ingress_name("config-sync") is True
        assert await client.call_host_ingress("config-sync", {"source": "host"}) == {
            "accepted": True,
            "payload": {"source": "host"},
        }
        assert observed == [{"source": "host"}]

        # A private ingress registration has no ToolMetadata and cannot be
        # surfaced through the agent-callable inventory.
        assert [tool.name for tool in await client.list_tools()] == ["agent-tool"]
    finally:
        await _close_client(client, service_task)


@pytest.mark.asyncio
async def test_sync_and_async_host_ingress_handlers_are_supported():
    service = IsolatedFeatureService(name="fake", version="1.0.0")

    async def asynchronous(payload):
        await asyncio.sleep(0)
        return {"kind": "async", "payload": payload}

    def synchronous(payload):
        return {"kind": "sync", "payload": payload}

    service.register_host_ingress("asynchronous", asynchronous)
    service.register_host_ingress_handler("synchronous", synchronous)
    client, service_task = await _ready_client(service)

    try:
        assert await client.call_host_ingress("asynchronous", 1) == {
            "kind": "async",
            "payload": 1,
        }
        assert await client.invoke_host_ingress("synchronous", ["host"]) == {
            "kind": "sync",
            "payload": ["host"],
        }
    finally:
        await _close_client(client, service_task)


@pytest.mark.asyncio
async def test_legacy_malformed_and_unknown_ingress_capabilities_fail_closed():
    legacy = IsolatedFeatureService(name="legacy", version="1.0.0")
    client, service_task = await _ready_client(legacy)

    try:
        assert HOST_INGRESS_CAPABILITY not in client.capabilities
        assert client.supports_host_ingress is False
        with pytest.raises(HostIngressUnsupportedError, match="not supported"):
            await client.call_host_ingress("host-event", {"ok": True})

        client.capabilities[HOST_INGRESS_CAPABILITY] = {"version": 1, "names": "bad"}
        assert client.host_ingress_capabilities is None
        with pytest.raises(HostIngressUnsupportedError, match="not supported"):
            await client.call_host_ingress("host-event", {"ok": True})
    finally:
        await _close_client(client, service_task)

    service = IsolatedFeatureService(name="current", version="1.0.0")
    service.register_host_ingress("host-event", lambda payload: {"ok": True})
    client, service_task = await _ready_client(service)
    try:
        with pytest.raises(HostIngressUnknownNameError, match="not available"):
            await client.call_host_ingress("not-advertised", {"ok": True})
        assert client.supports_host_ingress_name("not-advertised") is False
    finally:
        await _close_client(client, service_task)


@pytest.mark.asyncio
async def test_client_and_service_reject_malformed_or_oversized_payloads_safely():
    service = IsolatedFeatureService(name="fake", version="1.0.0")
    called = False

    def handler(payload):
        nonlocal called
        called = True
        return {"ok": True}

    service.register_host_ingress("host-event", handler)
    client, service_task = await _ready_client(service)
    secret = "host-secret-must-not-appear"

    try:
        with pytest.raises(HostIngressError) as client_error:
            await client.call_host_ingress(
                "host-event", {"secret": secret, "bad": object()}
            )
        assert str(client_error.value) == "host ingress failed"
        assert secret not in str(client_error.value)
        assert secret not in str(client_error.value.__cause__)
        assert called is False

        with pytest.raises(HostIngressError, match="host ingress failed"):
            await client.call_host_ingress(
                "host-event", "x" * MAX_HOST_INGRESS_PAYLOAD_BYTES
            )
        assert called is False

        # Bypass the typed client. The service must repeat boundary validation
        # and still return only the generic ingress envelope.
        with pytest.raises(ProtocolError) as service_error:
            await client.request(
                HOST_INGRESS,
                {
                    "name": "host-event",
                    "payload": {"secret": secret, "bad": float("nan")},
                },
            )
        assert str(service_error.value) == "host ingress failed"
        assert secret not in str(service_error.value)
        assert called is False

        with pytest.raises(ProtocolError, match="host ingress failed"):
            await client.request(
                HOST_INGRESS,
                {"name": "host-event", "payload": "x" * MAX_HOST_INGRESS_PAYLOAD_BYTES},
            )
        assert called is False
    finally:
        await _close_client(client, service_task)


@pytest.mark.asyncio
async def test_host_ingress_errors_do_not_reflect_handler_or_payload_secrets(caplog):
    service = IsolatedFeatureService(name="fake", version="1.0.0")
    secret = "private-host-token-123"

    async def failing(payload):
        raise RuntimeError(f"cannot process {payload['token']}")

    service.register_host_ingress("secret-event", failing)
    client, service_task = await _ready_client(service)

    try:
        with pytest.raises(HostIngressError) as error:
            await client.call_host_ingress("secret-event", {"token": secret})
        assert str(error.value) == "host ingress failed"
        assert secret not in str(error.value)
        assert secret not in str(error.value.__cause__)
        assert secret not in caplog.text
    finally:
        await _close_client(client, service_task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "implementation",
    ("async-handler", "sync-returned-awaitable", "override"),
)
async def test_cancelled_host_ingress_child_returns_generic_error_without_hanging(
    implementation: str,
):
    secret = "cancelled-host-ingress-secret"

    async def await_cancelled_child() -> None:
        async def cancelled_child() -> None:
            raise asyncio.CancelledError(secret)

        await asyncio.create_task(cancelled_child())

    if implementation == "override":

        class CancelledIngressService(IsolatedFeatureService):
            async def call_host_ingress(self, name, payload):
                await await_cancelled_child()
                raise AssertionError("cancelled child unexpectedly completed")

        service = CancelledIngressService(name="fake", version="1.0.0")

        def handler(payload):
            return {"ok": True}
    elif implementation == "sync-returned-awaitable":
        service = IsolatedFeatureService(name="fake", version="1.0.0")

        def handler(payload):
            return await_cancelled_child()

    else:
        service = IsolatedFeatureService(name="fake", version="1.0.0")

        async def handler(payload):
            await await_cancelled_child()
            raise AssertionError("cancelled child unexpectedly completed")

    service.register_host_ingress("host-event", handler)
    client, service_task = await _ready_client(service)

    try:
        with pytest.raises(ProtocolError) as error:
            await asyncio.wait_for(
                client.request(
                    HOST_INGRESS,
                    {"name": "host-event", "payload": {"secret": secret}},
                ),
                timeout=1,
            )
        assert str(error.value) == "host ingress failed"
        assert secret not in str(error.value)
        assert error.value.__cause__ is None
        assert await asyncio.wait_for(client.health(), timeout=1) == {
            "status": "ready",
            "ready": True,
        }
    finally:
        await _close_client(client, service_task)


@pytest.mark.asyncio
async def test_genuine_host_ingress_request_task_cancellation_still_propagates():
    service = IsolatedFeatureService(name="fake", version="1.0.0")
    entered = asyncio.Event()
    never = asyncio.Event()

    async def waiting_handler(payload):
        entered.set()
        await never.wait()
        return {"unreachable": True}

    service.register_host_ingress("host-event", waiting_handler)
    request_task = asyncio.create_task(
        service._handle_request(
            JsonRpcRequest(
                id=1,
                method=HOST_INGRESS,
                params={"name": "host-event", "payload": {}},
            )
        )
    )

    await asyncio.wait_for(entered.wait(), timeout=1)
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"secret": "override-result-secret", "invalid": float("nan")},
        "override-result-secret" + "x" * MAX_HOST_INGRESS_PAYLOAD_BYTES,
    ],
)
async def test_host_ingress_override_results_are_validated_and_redacted(result):
    class InvalidResultService(IsolatedFeatureService):
        async def call_host_ingress(self, name, payload):
            return result

    service = InvalidResultService(name="fake", version="1.0.0")
    service.register_host_ingress("host-event", lambda payload: {"ok": True})
    client, service_task = await _ready_client(service)
    request_secret = "override-request-secret"

    try:
        with pytest.raises(ProtocolError) as error:
            await client.request(
                HOST_INGRESS,
                {"name": "host-event", "payload": {"secret": request_secret}},
            )
        assert str(error.value) == "host ingress failed"
        assert request_secret not in str(error.value)
        assert "override-result-secret" not in str(error.value)
        assert error.value.__cause__ is None
    finally:
        await _close_client(client, service_task)


@pytest.mark.asyncio
async def test_raw_unregistered_ingress_cannot_bypass_override():
    class OverrideService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="fake", version="1.0.0")
            self.calls: list[tuple[str, object]] = []

        async def call_host_ingress(self, name, payload):
            self.calls.append((name, payload))
            return {"bypassed": True}

    service = OverrideService()
    service.register_host_ingress("registered", lambda payload: {"ok": True})
    client, service_task = await _ready_client(service)

    try:
        with pytest.raises(ProtocolError) as error:
            await client.request(
                HOST_INGRESS,
                {"name": "unregistered", "payload": {"secret": "host-only"}},
            )
        assert str(error.value) == "host ingress failed"
        assert service.calls == []
    finally:
        await _close_client(client, service_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True], ids=["omits-super", "raises"])
async def test_shutdown_latches_stopping_before_override_and_ends_serving(raises):
    host_reader, _host_writer, service_reader, service_writer = memory_stdio_pair()
    ingress_calls: list[object] = []

    class OverrideService(IsolatedFeatureService):
        async def on_shutdown(self):
            assert self._stopping is True
            if raises:
                raise RuntimeError("shutdown-failed")
            return {"cleaned": True}

    service = OverrideService(name="fake", version="1.0.0")
    service.register_host_ingress(
        "host-event", lambda payload: ingress_calls.append(payload) or {"ok": True}
    )
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))

    try:
        service_reader.feed(encode_message(JsonRpcRequest(id=1, method=SHUTDOWN)))
        response = decode_message(
            await asyncio.wait_for(host_reader.readline(), timeout=1)
        )
        assert response.id == 1
        if raises:
            assert response.error is not None
            assert response.error.message == "shutdown-failed"
        else:
            assert response.result == {"cleaned": True}

        await asyncio.wait_for(service_task, timeout=1)
        assert service._stopping is True
        with pytest.raises(ProtocolError, match="host ingress is unavailable"):
            await service._dispatch(
                JsonRpcRequest(
                    id=2,
                    method=HOST_INGRESS,
                    params={"name": "host-event", "payload": {"after": "shutdown"}},
                )
            )
        assert ingress_calls == []
    finally:
        if not service_task.done():
            service_reader.close()
            await asyncio.gather(service_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_blocking_sync_host_ingress_does_not_wedge_health_or_other_requests():
    host_reader, _host_writer, service_reader, service_writer = memory_stdio_pair()
    service = IsolatedFeatureService(name="fake", version="1.0.0")
    release = threading.Event()

    def blocking(payload):
        while not release.is_set():
            time.sleep(0.005)
        return {"done": True}

    async def ping(payload):
        return {"pong": payload}

    service.register_host_ingress("block", blocking)
    service.register_host_ingress("ping", ping)
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))

    async def next_response():
        return decode_message(await host_reader.readline())

    try:
        service_reader.feed(
            encode_message(
                JsonRpcRequest(
                    id=1, method=HOST_INGRESS, params={"name": "block", "payload": {}}
                )
            )
        )
        service_reader.feed(encode_message(JsonRpcRequest(id=2, method=HEALTH)))
        service_reader.feed(
            encode_message(
                JsonRpcRequest(
                    id=3,
                    method=HOST_INGRESS,
                    params={"name": "ping", "payload": "host"},
                )
            )
        )

        answers = {}
        for _ in range(2):
            response = await asyncio.wait_for(next_response(), timeout=2)
            answers[response.id] = response
        assert set(answers) == {2, 3}
        assert answers[2].result == {"status": "ready", "ready": True}
        assert answers[3].result == {"pong": "host"}

        release.set()
        assert (await asyncio.wait_for(next_response(), timeout=2)).result == {
            "done": True
        }
    finally:
        release.set()
        service._stopping = True
        service_reader.close()
        await asyncio.wait_for(service_task, timeout=2)


@pytest.mark.asyncio
async def test_host_ingress_is_fenced_after_shutdown_and_restart_required():
    service = IsolatedFeatureService(name="fake", version="1.0.0")
    service.register_host_ingress("host-event", lambda payload: {"ok": True})
    client, service_task = await _ready_client(service)

    try:
        await client.shutdown()
        await service_task
        with pytest.raises(HostIngressError, match="unavailable"):
            await client.call_host_ingress("host-event", {"after": "shutdown"})
        with pytest.raises(ProtocolError, match="host ingress is unavailable"):
            await service._dispatch(
                JsonRpcRequest(
                    id=1,
                    method=HOST_INGRESS,
                    params={"name": "host-event", "payload": {"after": "shutdown"}},
                )
            )
    finally:
        await client.close()

    class RestartService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="restart", version="1.0.0")
            self.advertise_config_transition()
            self.register_host_ingress("host-event", lambda payload: {"ok": True})

        async def on_config_transition(self, next_config):
            return ConfigTransitionResult.restart_required()

    service = RestartService()
    client, service_task = await _ready_client(service)
    try:
        assert await client.prepare_config_transition({"enabled": False}) == (
            ConfigTransitionResult.restart_required()
        )
        with pytest.raises(HostIngressError, match="unavailable"):
            await client.call_host_ingress("host-event", {"after": "restart"})
        with pytest.raises(ProtocolError, match="host ingress failed"):
            await client.request(
                HOST_INGRESS,
                {"name": "host-event", "payload": {"after": "restart"}},
            )
    finally:
        await _close_client(client, service_task)
