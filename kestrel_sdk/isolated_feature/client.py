"""Host-side client for isolated feature stdio JSON-RPC runtimes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
import asyncio

from .protocol import (
    FEATURE_EVENT,
    HEALTH,
    INITIALIZE,
    PROTOCOL_VERSION,
    SHUTDOWN,
    TOOLS_CALL,
    TOOLS_LIST,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    ProtocolError,
    ToolMetadata,
    decode_message,
    encode_message,
)

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class IsolatedFeatureClient:
    """Low-level client over connected line-oriented stdin/stdout streams."""

    def __init__(
        self,
        reader: Any,
        writer: Any,
        *,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.protocol_version = protocol_version
        self.server_info: dict[str, Any] | None = None
        self.capabilities: dict[str, Any] = {}
        self.tools: list[ToolMetadata] = []
        self.ready = False
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._event_handlers: list[EventHandler] = []
        self._read_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._read_task is None:
            self._read_task = asyncio.create_task(self._read_loop())

    def on_event(self, handler: EventHandler) -> None:
        self._event_handlers.append(handler)

    async def initialize(self, client_info: dict[str, Any] | None = None) -> dict[str, Any]:
        await self.start()
        result = await self.request(
            INITIALIZE,
            {
                "protocolVersion": self.protocol_version,
                "clientInfo": client_info or {"name": "kestrel-sdk-host"},
            },
        )
        if not isinstance(result, dict):
            raise ProtocolError("initialize result must be an object")
        if result.get("protocolVersion") != self.protocol_version:
            raise ProtocolError("service returned incompatible protocolVersion")
        self.server_info = _dict_field(result, "serverInfo")
        self.capabilities = _dict_field(result, "capabilities")
        return result

    async def health(self) -> dict[str, Any]:
        result = await self.request(HEALTH)
        if not isinstance(result, dict):
            raise ProtocolError("health result must be an object")
        self.ready = result.get("ready") is True or result.get("status") in {"ready", "ok"}
        return result

    async def list_tools(self) -> list[ToolMetadata]:
        if not self.ready:
            raise ProtocolError("health must report ready before tools are exposed")
        result = await self.request(TOOLS_LIST)
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            raise ProtocolError("tools/list result must contain a tools list")
        self.tools = [ToolMetadata.from_dict(item) for item in result["tools"]]
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if not self.ready:
            raise ProtocolError("health must report ready before tools can be called")
        return await self.request(
            TOOLS_CALL,
            {"name": name, "arguments": arguments or {}},
        )

    async def shutdown(self) -> dict[str, Any]:
        result = await self.request(SHUTDOWN)
        if not isinstance(result, dict):
            raise ProtocolError("shutdown result must be an object")
        return result

    async def close(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None
        close = getattr(self.writer, "close", None)
        if close is not None:
            close()
        wait_closed = getattr(self.writer, "wait_closed", None)
        if wait_closed is not None:
            await wait_closed()

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        await self.start()
        self._next_id += 1
        request_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        self.writer.write(encode_message(JsonRpcRequest(method=method, params=params or {}, id=request_id)))
        drain = getattr(self.writer, "drain", None)
        if drain is not None:
            await drain()
        return await future

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    raise EOFError("isolated feature stream closed")
                message = decode_message(line)
                if isinstance(message, JsonRpcResponse):
                    pending = self._pending.pop(message.id, None)
                    if pending is None:
                        continue
                    if message.error is not None:
                        pending.set_exception(ProtocolError(message.error.message))
                    else:
                        pending.set_result(message.result)
                elif isinstance(message, JsonRpcNotification):
                    await self._handle_notification(message)
        except Exception as exc:
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(exc)
            self._pending.clear()

    async def _handle_notification(self, notification: JsonRpcNotification) -> None:
        if notification.method != FEATURE_EVENT:
            return
        for handler in list(self._event_handlers):
            result = handler(notification.params)
            if asyncio.iscoroutine(result):
                await result


@dataclass
class SubprocessIsolatedFeatureClient:
    """Convenience wrapper that owns a feature child process lifecycle."""

    command: Sequence[str]
    env: dict[str, str] | None = None
    cwd: str | None = None
    protocol_version: str = PROTOCOL_VERSION
    ready_timeout: float = 10.0

    process: asyncio.subprocess.Process | None = None
    client: IsolatedFeatureClient | None = None

    async def start(self) -> None:
        if self.process is not None:
            return
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd=self.cwd,
        )
        if self.process.stdout is None or self.process.stdin is None:
            raise RuntimeError("subprocess stdio pipes were not created")
        self.client = IsolatedFeatureClient(
            self.process.stdout,
            self.process.stdin,
            protocol_version=self.protocol_version,
        )
        await self.client.initialize()
        await self._wait_until_ready()
        await self.client.list_tools()

    async def health(self) -> dict[str, Any]:
        return await self._require_client().health()

    async def list_tools(self) -> list[ToolMetadata]:
        return await self._require_client().list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await self._require_client().call_tool(name, arguments)

    def on_event(self, handler: EventHandler) -> None:
        self._require_client().on_event(handler)

    async def stop(self) -> None:
        process = self.process
        client = self.client
        if client is not None:
            try:
                await client.shutdown()
            finally:
                await client.close()
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        self.client = None
        self.process = None

    async def _wait_until_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.ready_timeout
        while True:
            health = await self._require_client().health()
            if health.get("ready") is True or health.get("status") in {"ready", "ok"}:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("isolated feature did not become ready")
            await asyncio.sleep(0.1)

    def _require_client(self) -> IsolatedFeatureClient:
        if self.client is None:
            raise RuntimeError("isolated feature client is not started")
        return self.client


def _dict_field(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ProtocolError(f"{key} must be an object")
    return value
