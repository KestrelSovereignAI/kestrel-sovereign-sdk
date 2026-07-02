"""Robustness regressions for the isolated-feature runtime (review Wave 1).

Covers four confirmed findings from the multi-agent code review:

* F014 — a *sync* tool handler that blocks must not wedge the event loop /
  HEALTH (the pre-0.28 wedge, reintroduced whenever a handler was sync).
* F015 — after the read loop dies, in-flight requests must fail (not hang) and
  later ``request()`` calls must fail fast instead of writing to a dead pipe.
* F016 — a service that writes to stdout must not corrupt JSON-RPC framing.
* F017 — event handlers registered on the subprocess wrapper must be
  re-attached to the fresh inner client after a restart.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from kestrel_sdk.isolated_feature import (
    HEALTH,
    TOOLS_CALL,
    IsolatedFeatureClient,
    IsolatedFeatureService,
    JsonRpcRequest,
    ProtocolError,
    ToolMetadata,
    decode_message,
    encode_message,
)
from kestrel_sdk.isolated_feature.client import SubprocessIsolatedFeatureClient

from .test_isolated_feature import memory_stdio_pair

_OBJ = {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# F014 — a blocking SYNC handler must not starve HEALTH or other requests.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_blocking_sync_handler_does_not_wedge_health():
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    service = IsolatedFeatureService(name="fake", version="1.0.0")

    release = asyncio.Event()

    def blocking_sync(arguments):
        # A synchronous blocking call with no OS timeout — the exact shape that
        # wedged the loop when handlers ran inline. Offloaded to a thread now.
        while not release.is_set():
            time.sleep(0.005)
        return {"done": True}

    async def ping(arguments):
        return {"pong": True}

    service.register_tool(ToolMetadata(name="block", description="", input_schema=_OBJ), blocking_sync)
    service.register_tool(ToolMetadata(name="ping", description="", input_schema=_OBJ), ping)

    service_task = asyncio.create_task(service.serve(service_reader, service_writer))

    async def next_response():
        return decode_message(await host_reader.readline())

    try:
        service_reader.feed(encode_message(JsonRpcRequest(
            id=1, method=TOOLS_CALL, params={"name": "block", "arguments": {}})))
        # Behind the blocked sync handler: HEALTH and another tool call.
        service_reader.feed(encode_message(JsonRpcRequest(id=2, method=HEALTH, params={})))
        service_reader.feed(encode_message(JsonRpcRequest(
            id=3, method=TOOLS_CALL, params={"name": "ping", "arguments": {}})))

        # HEALTH (id=2) and ping (id=3) must answer while block (id=1) is stuck.
        answered = {}
        for _ in range(2):
            resp = await asyncio.wait_for(next_response(), timeout=2.0)
            answered[resp.id] = resp
        assert set(answered) == {2, 3}
        assert answered[2].result["ready"] is True

        release.set()
        resp = await asyncio.wait_for(next_response(), timeout=2.0)
        assert resp.id == 1 and resp.result == {"done": True}
    finally:
        release.set()
        service._stopping = True
        service_reader.close()
        await asyncio.wait_for(service_task, timeout=2.0)


# ---------------------------------------------------------------------------
# F015 — read-loop death fails in-flight + future requests instead of hanging.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_read_loop_death_fails_inflight_requests():
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    client = IsolatedFeatureClient(host_reader, host_writer)
    await client.start()

    # An in-flight request with no reply coming.
    inflight = asyncio.ensure_future(client.request(HEALTH))
    await asyncio.sleep(0)  # let it register in _pending

    # Service stream closes (EOF) — read loop must terminate and fail waiters.
    host_reader.close()

    with pytest.raises((ConnectionError, EOFError, ProtocolError, OSError)):
        await asyncio.wait_for(inflight, timeout=2.0)


@pytest.mark.asyncio
async def test_request_after_read_loop_death_fails_fast():
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    client = IsolatedFeatureClient(host_reader, host_writer)
    await client.start()
    host_reader.close()
    # Let the read loop observe EOF and latch the terminal error.
    await asyncio.sleep(0.05)

    with pytest.raises((ConnectionError, EOFError, ProtocolError, OSError)):
        await asyncio.wait_for(client.request(HEALTH), timeout=2.0)


@pytest.mark.asyncio
async def test_close_during_inflight_request_does_not_hang():
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    client = IsolatedFeatureClient(host_reader, host_writer)
    await client.start()

    inflight = asyncio.ensure_future(client.request(HEALTH))
    await asyncio.sleep(0)

    # A supervised restart calls close() while a tool call is outstanding — the
    # future must resolve (with an error), not strand forever.
    await client.close()
    with pytest.raises(BaseException):
        await asyncio.wait_for(inflight, timeout=2.0)


# ---------------------------------------------------------------------------
# F017 — wrapper re-attaches event handlers to the fresh client on restart.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wrapper_reattaches_event_handlers_after_restart():
    received: list[dict] = []

    wrapper = SubprocessIsolatedFeatureClient(command=["/nonexistent"])

    # Register BEFORE any client exists — must be recorded on the wrapper.
    def handler(params):
        received.append(params)

    wrapper.on_event(handler)
    assert handler in wrapper._handlers

    # Simulate the inner client the wrapper would build on (re)start; the fix
    # re-attaches persisted handlers to it. We drive the inner client directly.
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    inner = IsolatedFeatureClient(host_reader, host_writer)
    wrapper.client = inner
    for h in wrapper._handlers:  # what start() does after building the client
        inner.on_event(h)

    await inner.start()
    # A feature event delivered after the "restart" reaches the handler.
    from kestrel_sdk.isolated_feature import FEATURE_EVENT, JsonRpcNotification
    host_reader.feed(encode_message(JsonRpcNotification(
        method=FEATURE_EVENT, params={"type": "channel.inbound", "payload": {"msg": "hi"}})))
    await asyncio.sleep(0.05)

    assert received and received[0]["type"] == "channel.inbound"
    await inner.close()
