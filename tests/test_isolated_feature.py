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
