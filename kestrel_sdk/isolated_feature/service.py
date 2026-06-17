"""Service-side base class for isolated feature runtimes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any
import asyncio
import sys

from .protocol import (
    FEATURE_EVENT,
    HEALTH,
    INITIALIZE,
    PROTOCOL_VERSION,
    SHUTDOWN,
    TOOLS_CALL,
    TOOLS_LIST,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    ProtocolError,
    ToolMetadata,
    decode_message,
    encode_message,
)

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any] | Any]


class IsolatedFeatureService:
    """Base service for stdio JSON-RPC isolated features."""

    def __init__(
        self,
        *,
        name: str,
        version: str,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> None:
        self.name = name
        self.version = version
        self.protocol_version = protocol_version
        self._tools: dict[str, ToolMetadata] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self._writer: Any = None
        self._stopping = False

    def register_tool(self, metadata: ToolMetadata, handler: ToolHandler) -> None:
        """Register one callable tool and its advertised metadata."""

        self._tools[metadata.name] = metadata
        self._handlers[metadata.name] = handler

    async def get_tools(self) -> list[ToolMetadata]:
        """Return tools exposed by this service."""

        return list(self._tools.values())

    async def health(self) -> dict[str, Any]:
        """Return service readiness. Override to include feature-specific checks."""

        return {"status": "ready", "ready": True}

    async def on_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if requested != self.protocol_version:
            raise ProtocolError(
                f"unsupported protocolVersion {requested!r}; expected {self.protocol_version!r}"
            )
        return {
            "protocolVersion": self.protocol_version,
            "serverInfo": {"name": self.name, "version": self.version},
            "capabilities": {"tools": True, "events": True},
        }

    async def on_shutdown(self) -> dict[str, Any]:
        self._stopping = True
        return {"ok": True}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Dispatch a tool call. Override for custom routing."""

        handler = self._handlers.get(name)
        if handler is None:
            raise ProtocolError(f"unknown tool: {name}")
        result = handler(arguments)
        if isawaitable(result):
            return await result
        return result

    async def emit_event(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Emit a service-to-host notification."""

        if self._writer is None:
            raise RuntimeError("service is not connected")
        await self._send(
            JsonRpcNotification(
                method=FEATURE_EVENT,
                params={"type": event_type, "payload": payload or {}},
            )
        )

    async def serve(self, reader: Any, writer: Any) -> None:
        """Serve JSON-RPC requests from a line-oriented reader/writer pair."""

        self._writer = writer
        try:
            while not self._stopping:
                line = await reader.readline()
                if not line:
                    break
                try:
                    message = decode_message(line)
                    if isinstance(message, JsonRpcRequest):
                        await self._handle_request(message)
                except ProtocolError as exc:
                    await self._send(
                        JsonRpcResponse(
                            id=None,
                            error=JsonRpcError(code=-32600, message=str(exc)),
                        )
                    )
        finally:
            self._writer = None

    async def run_stdio(self) -> None:
        """Run the service on process stdin/stdout."""

        reader = asyncio.StreamReader()
        loop = asyncio.get_running_loop()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        write_transport, write_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin,
            sys.stdout.buffer,
        )
        writer = asyncio.StreamWriter(write_transport, write_protocol, reader, loop)
        await self.serve(reader, writer)

    async def _handle_request(self, request: JsonRpcRequest) -> None:
        try:
            result = await self._dispatch(request)
            await self._send(JsonRpcResponse(id=request.id, result=result))
        except ProtocolError as exc:
            await self._send(
                JsonRpcResponse(
                    id=request.id,
                    error=JsonRpcError(code=-32602, message=str(exc)),
                )
            )
        except Exception as exc:
            await self._send(
                JsonRpcResponse(
                    id=request.id,
                    error=JsonRpcError(code=-32603, message=str(exc)),
                )
            )

    async def _dispatch(self, request: JsonRpcRequest) -> Any:
        if request.method == INITIALIZE:
            return await self.on_initialize(request.params)
        if request.method == HEALTH:
            return await self.health()
        if request.method == TOOLS_LIST:
            return {"tools": [tool.to_dict() for tool in await self.get_tools()]}
        if request.method == TOOLS_CALL:
            name = request.params.get("name")
            if not isinstance(name, str) or not name:
                raise ProtocolError("tools/call requires name")
            arguments = request.params.get("arguments", {})
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise ProtocolError("tools/call arguments must be an object")
            return await self.call_tool(name, arguments)
        if request.method == SHUTDOWN:
            return await self.on_shutdown()
        raise ProtocolError(f"unknown method: {request.method}")

    async def _send(self, message: JsonRpcResponse | JsonRpcNotification) -> None:
        self._writer.write(encode_message(message))
        drain = getattr(self._writer, "drain", None)
        if drain is not None:
            await drain()
