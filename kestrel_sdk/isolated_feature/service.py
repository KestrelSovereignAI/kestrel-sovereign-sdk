"""Service-side base class for isolated feature runtimes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable, iscoroutinefunction
from typing import Any
import asyncio
import os
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


def _is_async_callable(handler: ToolHandler) -> bool:
    """True if calling ``handler`` returns a coroutine to await.

    Covers plain ``async def`` functions, ``functools.partial`` around them
    (``iscoroutinefunction`` unwraps partials), and callable instances whose
    ``__call__`` is ``async def``. Everything else is treated as sync and
    offloaded to a thread in :meth:`IsolatedFeatureService.call_tool`.
    """

    if iscoroutinefunction(handler):
        return True
    call = getattr(handler, "__call__", None)
    return call is not None and iscoroutinefunction(call)


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
        # Whether each handler is a native coroutine function. Sync handlers are
        # offloaded to a worker thread in ``call_tool`` so a blocking body can
        # never wedge the event loop (and with it HEALTH, which the supervisor
        # polls). Decided once at registration rather than per call.
        self._handler_is_async: dict[str, bool] = {}
        self._writer: Any = None
        self._stopping = False
        # Requests are handled concurrently (see ``serve``); serialize the actual
        # writes so two in-flight handlers can't interleave bytes on the wire.
        self._write_lock = asyncio.Lock()
        self._inflight: set[asyncio.Task[Any]] = set()
        # Host-provided configuration delivered through the initialize handshake.
        # Empty until the host calls initialize with a ``config`` param, at which
        # point it is populated and ``configure()`` runs.
        self.host_config: dict[str, Any] = {}
        self._channel_capability: dict[str, Any] | None = None

    def advertise_channel(
        self,
        *,
        channel_type: str,
        send_tool: str,
        status_tool: str | None = None,
    ) -> None:
        """Declare that this service backs a messaging channel.

        The host bridges this into ``ChannelFeature.registry`` as a forwarding
        adapter so the generic channels API (``channels_send``/``channels_list``)
        works against an isolated channel feature. ``send_tool`` is the
        registered tool the adapter calls to send (it must accept ``to`` and
        ``message`` arguments); ``status_tool`` is an optional tool used to
        report live link state.
        """

        capability: dict[str, Any] = {
            "channel_type": channel_type,
            "send_tool": send_tool,
        }
        if status_tool is not None:
            capability["status_tool"] = status_tool
        self._channel_capability = capability

    def register_tool(self, metadata: ToolMetadata, handler: ToolHandler) -> None:
        """Register one callable tool and its advertised metadata."""

        self._tools[metadata.name] = metadata
        self._handlers[metadata.name] = handler
        self._handler_is_async[metadata.name] = _is_async_callable(handler)

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
        config = params.get("config")
        if isinstance(config, dict):
            self.host_config = config
            await self.configure(config)
        capabilities: dict[str, Any] = {"tools": True, "events": True}
        if self._channel_capability is not None:
            capabilities["channel"] = dict(self._channel_capability)
        return {
            "protocolVersion": self.protocol_version,
            "serverInfo": {"name": self.name, "version": self.version},
            "capabilities": capabilities,
        }

    async def configure(self, config: dict[str, Any]) -> None:
        """Apply host-provided configuration from the initialize handshake.

        Override to consume persisted/UI feature config that the host forwards
        (the service is launched as a bare process, so this handshake is the
        only path for non-environment configuration). Default is a no-op.
        """

    async def on_shutdown(self) -> dict[str, Any]:
        self._stopping = True
        return {"ok": True}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Dispatch a tool call. Override for custom routing."""

        handler = self._handlers.get(name)
        if handler is None:
            raise ProtocolError(f"unknown tool: {name}")
        if self._handler_is_async.get(name):
            # Native coroutine handler: it owns its own await points and must
            # not block between them (documented contract).
            return await handler(arguments)
        # Sync handler: run it on a worker thread so a blocking call inside it
        # (network connect without an OS timeout, subprocess, time.sleep) can
        # never freeze the event loop and starve HEALTH / other concurrent
        # requests. Preserve the legacy "sync function returns an awaitable"
        # shape by awaiting the result back on the loop.
        result = await asyncio.to_thread(handler, arguments)
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
                except ProtocolError as exc:
                    await self._send(
                        JsonRpcResponse(
                            id=None,
                            error=JsonRpcError(code=-32600, message=str(exc)),
                        )
                    )
                    continue
                if isinstance(message, JsonRpcRequest):
                    if message.method in (INITIALIZE, SHUTDOWN):
                        # Handle inline (not concurrently):
                        #  * INITIALIZE must finish the handshake + apply host
                        #    config before any later request runs, or a pipelined
                        #    health/tools-call could hit an uninitialized service;
                        #  * SHUTDOWN is terminal and sets ``_stopping`` — inline
                        #    so the loop observes the flag and exits promptly (a
                        #    task would set it only after we'd re-blocked on read).
                        await self._handle_request(message)
                        continue
                    # Handle every other request concurrently: awaiting each
                    # handler inline would let a single slow/stuck tool call (e.g.
                    # a blocked network connect) starve every other request —
                    # including HEALTH, which the host supervisor polls, so the
                    # whole service (and its caller) would wedge silently.
                    # Responses are id-matched, so out-of-order completion is
                    # fine; ``_send`` serializes the bytes.
                    task = asyncio.ensure_future(self._handle_request(message))
                    self._inflight.add(task)
                    task.add_done_callback(self._inflight.discard)
        finally:
            # Give in-flight handlers a brief grace period to finish so their
            # responses aren't dropped, then CANCEL any still stuck — otherwise a
            # blocked handler (the exact stuck-network case this fix tolerates)
            # would keep ``serve()`` from ever returning on shutdown.
            pending = list(self._inflight)
            if pending:
                _, still = await asyncio.wait(pending, timeout=2.0)
                for task in still:
                    task.cancel()
                if still:
                    await asyncio.gather(*still, return_exceptions=True)
            self._writer = None

    async def run_stdio(self) -> None:
        """Run the service on process stdin/stdout."""

        reader = asyncio.StreamReader()
        loop = asyncio.get_running_loop()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        # Claim a PRIVATE duplicate of the real stdout fd for the JSON-RPC wire,
        # then repoint the process's stdout (fd 1 and sys.stdout) at stderr.
        # After this, a stray print(), C-extension banner, or dependency
        # deprecation notice written to stdout lands on the inherited stderr as
        # a harmless log line instead of corrupting protocol framing and killing
        # the connection. The protocol owns a fd nothing else can reach.
        wire_fd = os.dup(1)
        wire = os.fdopen(wire_fd, "wb", buffering=0)
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        write_transport, write_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin,
            wire,
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
        # Serialize concurrent writes (handlers now run concurrently) so a
        # message's bytes + drain complete before another's begin — otherwise two
        # handlers could interleave lines / drains on the same writer.
        async with self._write_lock:
            writer = self._writer
            if writer is None:
                return
            writer.write(encode_message(message))
            drain = getattr(writer, "drain", None)
            if drain is not None:
                await drain()
