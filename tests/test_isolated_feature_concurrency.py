"""A slow tool handler must not wedge the whole isolated-feature service.

Regression for the live hang: the service ``serve()`` loop processed requests
SERIALLY (awaited each handler before reading the next line), so one blocked
tool call (e.g. a WhatsApp connect stuck on the network) hung every subsequent
request — including HEALTH, which the host supervisor polls with no timeout, so
the whole service (and the caller) wedged silently with no restart.

Driven at the ``serve()`` loop directly (raw JSON-RPC lines) so the assertion is
about the loop's concurrency, not the client's.
"""

from __future__ import annotations

import asyncio

import pytest

from kestrel_sdk.isolated_feature import (
    HEALTH,
    TOOLS_CALL,
    IsolatedFeatureService,
    JsonRpcRequest,
    ToolMetadata,
    decode_message,
    encode_message,
)

from .test_isolated_feature import memory_stdio_pair

_OBJ = {"type": "object", "properties": {}}


@pytest.mark.asyncio
async def test_slow_tool_does_not_block_health_or_other_requests():
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    service = IsolatedFeatureService(name="fake", version="1.0.0")

    release = asyncio.Event()

    async def slow(arguments):
        await release.wait()
        return {"done": True}

    async def ping(arguments):
        return {"pong": True}

    service.register_tool(ToolMetadata(name="slow", description="", input_schema=_OBJ), slow)
    service.register_tool(ToolMetadata(name="ping", description="", input_schema=_OBJ), ping)

    service_task = asyncio.create_task(service.serve(service_reader, service_writer))

    async def next_response():
        return decode_message(await host_reader.readline())

    try:
        # Fire a tool call that blocks indefinitely (a stuck connect), then a
        # HEALTH request behind it.
        service_reader.feed(encode_message(JsonRpcRequest(
            id=1, method=TOOLS_CALL, params={"name": "slow", "arguments": {}})))
        service_reader.feed(encode_message(JsonRpcRequest(id=2, method=HEALTH)))

        # HEALTH (id=2) must come back while slow (id=1) is still blocked — a
        # serial loop would starve it and this times out.
        resp = await asyncio.wait_for(next_response(), timeout=2.0)
        assert resp.id == 2, f"expected health(id=2) first, got id={resp.id}"

        # A second tool call is likewise not blocked.
        service_reader.feed(encode_message(JsonRpcRequest(
            id=3, method=TOOLS_CALL, params={"name": "ping", "arguments": {}})))
        resp = await asyncio.wait_for(next_response(), timeout=2.0)
        assert resp.id == 3 and resp.result == {"pong": True}

        # Release the slow handler — its (late) response still arrives.
        release.set()
        resp = await asyncio.wait_for(next_response(), timeout=2.0)
        assert resp.id == 1 and resp.result == {"done": True}
    finally:
        release.set()
        service._stopping = True
        service_reader.close()
        service_task.cancel()
        await asyncio.gather(service_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_terminates_even_with_a_stuck_handler():
    """A blocked handler must not keep serve() from returning on shutdown."""
    from kestrel_sdk.isolated_feature import SHUTDOWN

    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    service = IsolatedFeatureService(name="fake", version="1.0.0")

    never = asyncio.Event()  # never set — the handler is permanently stuck

    async def stuck(arguments):
        await never.wait()
        return {}

    service.register_tool(ToolMetadata(name="stuck", description="", input_schema=_OBJ), stuck)
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    try:
        service_reader.feed(encode_message(JsonRpcRequest(
            id=1, method=TOOLS_CALL, params={"name": "stuck", "arguments": {}})))
        await asyncio.sleep(0.05)  # let it reach the stuck handler
        service_reader.feed(encode_message(JsonRpcRequest(id=2, method=SHUTDOWN)))

        # serve() must return (grace period, then cancel the stuck handler) —
        # not hang forever waiting on the blocked task.
        await asyncio.wait_for(service_task, timeout=6.0)
    finally:
        never.set()
        if not service_task.done():
            service_task.cancel()
            await asyncio.gather(service_task, return_exceptions=True)
