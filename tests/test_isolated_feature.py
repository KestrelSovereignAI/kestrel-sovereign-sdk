"""Tests for the isolated feature JSON-RPC stdio contract."""

from __future__ import annotations

import asyncio

import pytest

from kestrel_sdk.isolated_feature import (
    FEATURE_EVENT,
    PROTOCOL_VERSION,
    IsolatedFeatureClient,
    IsolatedFeatureService,
    JsonRpcNotification,
    ProtocolError,
    ToolMetadata,
    decode_message,
    encode_message,
)


class MemoryReader:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self.queue.get()

    def feed(self, data: bytes) -> None:
        self.queue.put_nowait(data)

    def close(self) -> None:
        self.queue.put_nowait(b"")


class MemoryWriter:
    def __init__(self, peer: MemoryReader) -> None:
        self.peer = peer
        self.closed = False

    def write(self, data: bytes) -> None:
        for line in data.splitlines(keepends=True):
            self.peer.feed(line)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True
        self.peer.close()

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


def memory_stdio_pair() -> tuple[MemoryReader, MemoryWriter, MemoryReader, MemoryWriter]:
    host_reader = MemoryReader()
    service_reader = MemoryReader()
    host_writer = MemoryWriter(service_reader)
    service_writer = MemoryWriter(host_reader)
    return host_reader, host_writer, service_reader, service_writer


@pytest.mark.asyncio
async def test_service_client_lifecycle_tools_and_events():
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    service = IsolatedFeatureService(name="fake-feature", version="1.0.0")

    async def echo(arguments):
        await service.emit_event("tool-called", {"name": "echo"})
        return {"content": [{"type": "text", "text": arguments["text"]}]}

    service.register_tool(
        ToolMetadata(
            name="echo",
            description="Echo text",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            skill_id="fake.echo",
            version="1.0.0",
            capability_tags=("test",),
            permission_tags=("local",),
            category="utility",
            command_prefix="@fake",
        ),
        echo,
    )

    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)
    events: list[dict] = []
    client.on_event(events.append)

    try:
        initialized = await client.initialize({"name": "test-host"})
        assert initialized["protocolVersion"] == PROTOCOL_VERSION
        assert initialized["serverInfo"] == {"name": "fake-feature", "version": "1.0.0"}
        assert initialized["capabilities"]["tools"] is True

        assert await client.health() == {"status": "ready", "ready": True}

        tools = await client.list_tools()
        assert tools == [
            ToolMetadata(
                name="echo",
                description="Echo text",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                skill_id="fake.echo",
                version="1.0.0",
                capability_tags=("test",),
                permission_tags=("local",),
                category="utility",
                command_prefix="@fake",
            )
        ]

        result = await client.call_tool("echo", {"text": "hello"})
        assert result == {"content": [{"type": "text", "text": "hello"}]}

        await asyncio.sleep(0)
        assert events == [{"type": "tool-called", "payload": {"name": "echo"}}]

        assert await client.shutdown() == {"ok": True}
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unknown_tool_returns_json_rpc_error():
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    service = IsolatedFeatureService(name="fake-feature", version="1.0.0")
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        await client.initialize()
        await client.health()
        with pytest.raises(ProtocolError, match="unknown tool"):
            await client.call_tool("missing", {})
        await client.shutdown()
        await service_task
    finally:
        await client.close()


def test_protocol_rejects_non_object_tool_schema():
    with pytest.raises(ProtocolError, match="input_schema"):
        ToolMetadata.from_dict({"name": "bad", "description": "bad", "input_schema": []})


@pytest.mark.asyncio
async def test_tools_are_gated_until_health_reports_ready():
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    service = IsolatedFeatureService(name="fake-feature", version="1.0.0")
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        await client.initialize()
        with pytest.raises(ProtocolError, match="health must report ready"):
            await client.list_tools()
        await client.health()
        assert await client.list_tools() == []
        await client.shutdown()
        await service_task
    finally:
        await client.close()


def test_feature_event_notification_round_trip():
    message = decode_message(
        encode_message(JsonRpcNotification(method=FEATURE_EVENT, params={"type": "ready"}))
    )
    assert isinstance(message, JsonRpcNotification)
    assert message.method == FEATURE_EVENT
    assert message.params == {"type": "ready"}


@pytest.mark.asyncio
async def test_events_before_handler_are_buffered_and_flushed():
    """Feature events emitted before the host subscribes are buffered, not lost,
    and delivered to the first handler registered."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    service = IsolatedFeatureService(name="fake-feature", version="1.0.0")
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        await client.initialize()
        # emit BEFORE any handler is registered
        await service.emit_event("channel.inbound", {"content": "early"})
        await asyncio.sleep(0)  # let the host read loop buffer it

        received: list[dict] = []
        delivered = asyncio.Event()

        def handler(params):
            received.append(params)
            delivered.set()

        client.on_event(handler)  # registering flushes the buffer (scheduled task)
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert received == [{"type": "channel.inbound", "payload": {"content": "early"}}]

        await client.shutdown()
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_buffered_events_only_replay_to_first_handler():
    """A handler registered after the first must not receive pre-subscribe events."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    service = IsolatedFeatureService(name="fake-feature", version="1.0.0")
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        await client.initialize()
        await service.emit_event("e", {"n": 1})
        await asyncio.sleep(0)

        first: list = []
        second: list = []
        got = asyncio.Event()
        client.on_event(lambda p: (first.append(p), got.set()))
        client.on_event(second.append)  # late subscriber
        await asyncio.wait_for(got.wait(), timeout=1)
        await asyncio.sleep(0)
        assert len(first) == 1
        assert second == []  # buffered event not replayed to the late handler

        await client.shutdown()
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_buffered_events_preserve_stream_order():
    """Buffered startup events are delivered before later live events, in order."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    service = IsolatedFeatureService(name="fake-feature", version="1.0.0")
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        await client.initialize()
        await service.emit_event("e", {"n": 1})
        await service.emit_event("e", {"n": 2})
        await asyncio.sleep(0)  # buffered (no handler yet)

        received: list[int] = []
        done = asyncio.Event()

        def handler(params):
            received.append(params["payload"]["n"])
            if params["payload"]["n"] == 3:
                done.set()

        client.on_event(handler)
        await service.emit_event("e", {"n": 3})  # live event after registration
        await asyncio.wait_for(done.wait(), timeout=1)
        assert received == [1, 2, 3]

        await client.shutdown()
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_initialize_forwards_host_config_to_service():
    """Host config in the initialize handshake reaches the service and drives configure()."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()

    applied: list[dict] = []

    class ConfigurableService(IsolatedFeatureService):
        async def configure(self, config):
            applied.append(config)

    service = ConfigurableService(name="fake-feature", version="1.0.0")
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        await client.initialize(config={"provider": "web", "allowed_senders": ["+1303"]})
        assert applied == [{"provider": "web", "allowed_senders": ["+1303"]}]
        assert service.host_config == {"provider": "web", "allowed_senders": ["+1303"]}
        await client.shutdown()
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_service_advertises_channel_capability():
    """A channel-backing service surfaces its bridge metadata via initialize capabilities."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()

    service = IsolatedFeatureService(name="wa-feature", version="1.0.0")
    service.advertise_channel(
        channel_type="whatsapp",
        send_tool="whatsapp_send",
        status_tool="whatsapp_status",
    )
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        initialized = await client.initialize()
        assert initialized["capabilities"]["channel"] == {
            "channel_type": "whatsapp",
            "send_tool": "whatsapp_send",
            "status_tool": "whatsapp_status",
        }
        # client caches capabilities for the host-side bridge
        assert client.capabilities["channel"]["channel_type"] == "whatsapp"
        await client.shutdown()
        await service_task
    finally:
        await client.close()
