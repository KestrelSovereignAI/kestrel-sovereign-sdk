"""Host-side client for isolated feature stdio JSON-RPC runtimes."""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
import asyncio

from .protocol import (
    CONFIG_TRANSITION,
    CONFIG_TRANSITION_APPLIED,
    CONFIG_TRANSITION_CAPABILITY,
    FEATURE_EVENT,
    HEALTH,
    INITIALIZE,
    PROTOCOL_VERSION,
    SHUTDOWN,
    TOOLS_CALL,
    TOOLS_LIST,
    ConfigTransitionCapabilities,
    ConfigTransitionError,
    ConfigTransitionResult,
    ConfigTransitionUnsupportedError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    ProtocolError,
    ToolMetadata,
    decode_message,
    encode_message,
)

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]

# Cap on feature events buffered before any handler subscribes, so a host that
# only uses tools (never calls on_event) can't accumulate events unbounded.
# Oldest events are dropped past this — the buffer exists to cover the brief
# startup window, not to be a durable queue.
_MAX_PENDING_EVENTS = 256


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
        # Latched once the read loop terminates (EOF, decode error, cancel).
        # After this is set the stream is dead: request()/start() fail fast with
        # it instead of writing to a broken pipe and awaiting a reply that can
        # never arrive.
        self._closed_exc: BaseException | None = None
        # Notifications that arrive before any handler is registered are buffered
        # and flushed to the first handler. A service may emit feature events
        # (e.g. channel.inbound from an already-linked session) during startup,
        # before the host has called on_event — without this they would be lost.
        self._pending_notifications: deque[dict[str, Any]] = deque(maxlen=_MAX_PENDING_EVENTS)
        # Serializes event delivery so a buffered-event flush cannot interleave
        # with (or reorder relative to) live notifications from the read loop.
        self._event_lock = asyncio.Lock()
        # Host lifecycle operations are deliberately serialized locally. If a
        # config transition gets the lock first, shutdown waits for its explicit
        # success/failure; if shutdown gets it first, a later transition fails
        # locally instead of writing a request to a process being torn down.
        self._lifecycle_lock = asyncio.Lock()
        self._shutdown_started = False
        # Latched after a prepare-only result, or when a transition is
        # interrupted after it may have reached the child. In either case the
        # caller must replace this process before sending further work. The
        # latter is deliberately conservative: cancellation cannot retract an
        # already-written JSON-RPC request, so the child may still retire its
        # old resources after the caller has stopped waiting.
        self._restart_required = False

    async def start(self) -> None:
        if self._read_task is None:
            self._read_task = asyncio.create_task(self._read_loop())

    def on_event(self, handler: EventHandler) -> None:
        first_handler = not self._event_handlers
        self._event_handlers.append(handler)
        if first_handler and self._pending_notifications:
            asyncio.create_task(self._drain_pending())

    async def _drain_pending(self) -> None:
        async with self._event_lock:
            await self._flush_buffer()

    async def _flush_buffer(self) -> None:
        """Deliver buffered (pre-subscribe) events to current handlers, in order.

        Buffering covers the startup window before any handler is registered;
        handlers SHOULD subscribe immediately after start. Events emitted while
        no handler was registered are delivered to the handlers present when the
        buffer flushes (best effort — the wire/read-loop boundary between
        "buffered" and "live" is not otherwise observable).
        """
        buffered = self._pending_notifications
        self._pending_notifications = deque(maxlen=_MAX_PENDING_EVENTS)
        for params in buffered:
            await self._dispatch_event(params)

    async def _dispatch_event(self, params: dict[str, Any]) -> None:
        for handler in list(self._event_handlers):
            result = handler(params)
            if asyncio.iscoroutine(result):
                await result

    async def initialize(
        self,
        client_info: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.start()
        params: dict[str, Any] = {
            "protocolVersion": self.protocol_version,
            "clientInfo": client_info or {"name": "kestrel-sdk-host"},
        }
        if config is not None:
            # Forward host-side feature config so it reaches the isolated service,
            # which is otherwise launched with no configuration but env vars.
            params["config"] = config
        result = await self.request(INITIALIZE, params)
        if not isinstance(result, dict):
            raise ProtocolError("initialize result must be an object")
        if result.get("protocolVersion") != self.protocol_version:
            raise ProtocolError("service returned incompatible protocolVersion")
        self.server_info = _dict_field(result, "serverInfo")
        self.capabilities = _dict_field(result, "capabilities")
        return result

    @property
    def config_transition_capabilities(self) -> ConfigTransitionCapabilities | None:
        """Return negotiated transition support, or ``None`` for legacy services.

        A malformed capability is treated as unsupported. This lets a host use
        the conservative fallback (replace without a transition request) rather
        than sending a lifecycle RPC to an unknown or incorrectly advertising
        service.
        """

        capability = self.capabilities.get(CONFIG_TRANSITION_CAPABILITY)
        if not isinstance(capability, dict):
            return None
        try:
            return ConfigTransitionCapabilities.from_dict(capability)
        except ProtocolError:
            return None

    @property
    def supports_config_transition(self) -> bool:
        """Whether the initialized service explicitly supports transitions."""

        return self.config_transition_capabilities is not None

    async def prepare_config_transition(
        self, next_config: dict[str, Any]
    ) -> ConfigTransitionResult:
        """Run the host-only pre-restart/live-apply transition lifecycle hook.

        The service receives ``next_config`` while its ``host_config`` still
        names the old effective configuration. A ``restart`` result requires
        the host to replace the process; ``applied`` permits the host to retain
        it. Hook failures raise :class:`ConfigTransitionError` without exposing
        configuration or service exception text. Cancelling an in-flight call
        re-raises the cancellation but fences this client for replacement,
        because the child may still finish the request already on the wire.
        """

        if not isinstance(next_config, dict):
            raise ConfigTransitionError("next config must be an object")
        if self.config_transition_capabilities is None:
            raise ConfigTransitionUnsupportedError(
                "service does not advertise config transition support"
            )

        async with self._lifecycle_lock:
            if self._shutdown_started:
                raise ConfigTransitionError("service shutdown is already in progress")
            if self._restart_required:
                raise ConfigTransitionError("service replacement is already required")
            capabilities = self.config_transition_capabilities
            if capabilities is None:
                raise ConfigTransitionUnsupportedError(
                    "service does not advertise config transition support"
                )
            try:
                result = await self.request(CONFIG_TRANSITION, {"config": next_config})
            except asyncio.CancelledError:
                # A cancelled local wait does not cancel the request already on
                # the wire. Retire this client instance so a supervisor cannot
                # issue a conflicting transition or tool call while the remote
                # outcome is unknown; it should stop and replace the child.
                self._mark_restart_required()
                raise
            except ProtocolError as exc:
                # The service intentionally gives config lifecycle failures a
                # generic JSON-RPC error; retain that boundary at this typed API.
                raise ConfigTransitionError("config transition failed") from exc
            except Exception as exc:
                # A child exit or broken stdio stream is a typed lifecycle
                # failure for callers. The process cannot be trusted after a
                # request transport failure, so require replacement as well.
                self._mark_restart_required()
                raise ConfigTransitionError("config transition failed") from exc

            try:
                if not isinstance(result, dict):
                    raise ProtocolError("config transition result must be an object")
                transition = ConfigTransitionResult.from_dict(result)
            except ProtocolError as exc:
                # A malformed success response leaves the child's effective
                # state unknown, unlike a normal generic hook-failure envelope.
                self._mark_restart_required()
                raise ConfigTransitionError("config transition failed") from exc

            if (
                transition.action == CONFIG_TRANSITION_APPLIED
                and not capabilities.supports_live_apply
            ):
                self._mark_restart_required()
                raise ConfigTransitionError(
                    "service returned an unadvertised config transition action"
                )
            if transition.action != CONFIG_TRANSITION_APPLIED:
                self._mark_restart_required()
            return transition

    async def health(self) -> dict[str, Any]:
        if self._restart_required:
            self.ready = False
            return {"status": "restart-required", "ready": False}
        result = await self.request(HEALTH)
        if not isinstance(result, dict):
            raise ProtocolError("health result must be an object")
        self.ready = result.get("ready") is True or result.get("status") in {"ready", "ok"}
        return result

    async def list_tools(self) -> list[ToolMetadata]:
        if self._restart_required:
            raise ProtocolError("service replacement is required before tools are exposed")
        if not self.ready:
            raise ProtocolError("health must report ready before tools are exposed")
        result = await self.request(TOOLS_LIST)
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            raise ProtocolError("tools/list result must contain a tools list")
        self.tools = [ToolMetadata.from_dict(item) for item in result["tools"]]
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if self._restart_required:
            raise ProtocolError("service replacement is required before tools can be called")
        if not self.ready:
            raise ProtocolError("health must report ready before tools can be called")
        return await self.request(
            TOOLS_CALL,
            {"name": name, "arguments": arguments or {}},
        )

    async def shutdown(self) -> dict[str, Any]:
        async with self._lifecycle_lock:
            if self._shutdown_started:
                raise ProtocolError("shutdown is already in progress")
            self._shutdown_started = True
            result = await self.request(SHUTDOWN)
            if not isinstance(result, dict):
                raise ProtocolError("shutdown result must be an object")
            return result

    async def close(self) -> None:
        # Prevent a later public transition call from starting while the stream
        # is being closed by a supervisor.
        self._shutdown_started = True
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
        # Fail fast on a dead stream rather than writing to a broken pipe and
        # awaiting a reply that will never come (the wedge this fixes).
        if self._closed_exc is not None:
            raise self._closed_exc
        self._next_id += 1
        request_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        # Re-check after registering: if the read loop died between the guard
        # above and now, its terminal drain already ran and would never see this
        # future — resolve it against the latched error instead of hanging.
        if self._closed_exc is not None:
            self._pending.pop(request_id, None)
            raise self._closed_exc
        try:
            self.writer.write(encode_message(JsonRpcRequest(method=method, params=params or {}, id=request_id)))
            drain = getattr(self.writer, "drain", None)
            if drain is not None:
                await drain()
            return await future
        finally:
            # Cancellation (including ``asyncio.wait_for`` timeouts) cancels
            # this waiter but does not cancel the response arriving from the
            # child. Remove it so the read loop can harmlessly discard that
            # late response instead of trying ``set_result`` on a cancelled
            # future and dying with InvalidStateError.
            if self._pending.get(request_id) is future:
                self._pending.pop(request_id, None)

    async def _read_loop(self) -> None:
        # Default terminal cause if the loop somehow exits without raising.
        exc: BaseException = ConnectionError("isolated feature stream closed")
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
                        if not pending.done():
                            pending.set_exception(ProtocolError(message.error.message))
                    elif not pending.done():
                        pending.set_result(message.result)
                elif isinstance(message, JsonRpcNotification):
                    await self._handle_notification(message)
        except asyncio.CancelledError as cancelled:
            # close() cancels us deliberately; still fail in-flight requests
            # (see finally) so a tool call outstanding during a restart doesn't
            # hang forever, then propagate so the task ends cancelled as awaited.
            exc = cancelled
            raise
        except Exception as loop_exc:  # noqa: BLE001 — terminal; surfaced via futures
            exc = loop_exc
        finally:
            # Latch the terminal condition and fail EVERY pending request so no
            # waiter is stranded, and so later request() calls fail fast.
            self._closed_exc = exc
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(exc)
            self._pending.clear()

    def _mark_restart_required(self) -> None:
        """Fence local work after a child restart outcome becomes possible."""

        self._restart_required = True
        self.ready = False

    async def _handle_notification(self, notification: JsonRpcNotification) -> None:
        if notification.method != FEATURE_EVENT:
            return
        async with self._event_lock:
            if not self._event_handlers:
                # Buffer until a handler is registered (see on_event), so startup
                # events emitted before the host subscribes are not dropped.
                self._pending_notifications.append(notification.params)
                return
            # Deliver any still-buffered events first so stream order is kept even
            # if a live event arrives before _drain_pending runs.
            if self._pending_notifications:
                await self._flush_buffer()
            await self._dispatch_event(notification.params)


@dataclass
class SubprocessIsolatedFeatureClient:
    """Convenience wrapper that owns a feature child process lifecycle."""

    command: Sequence[str]
    env: dict[str, str] | None = None
    cwd: str | None = None
    protocol_version: str = PROTOCOL_VERSION
    ready_timeout: float = 10.0
    # Appended after the existing fields to keep the dataclass-generated
    # positional constructor backward-compatible for feature packages.
    config: dict[str, Any] | None = None

    process: asyncio.subprocess.Process | None = None
    client: IsolatedFeatureClient | None = None
    # Event handlers registered via on_event. Persisted on the wrapper (which
    # survives restarts) rather than only on the inner client (which is rebuilt
    # on every start), so a supervised restart re-attaches them to the fresh
    # client instead of silently dropping every subsequent feature event.
    _handlers: list[EventHandler] = field(default_factory=list)

    async def start(self) -> None:
        if self.process is not None:
            return
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Inherit the parent's stderr rather than PIPE: a service that logs
            # to stderr would otherwise fill an unread pipe buffer and block,
            # hanging the JSON-RPC protocol on stdout. Inheriting keeps the
            # service's logs visible to the host and removes the deadlock.
            stderr=None,
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
        # Re-attach persisted handlers BEFORE initialize so the inner client's
        # startup-event buffer flushes to them (a relinked service may emit
        # channel.inbound during the handshake). On a first start this list is
        # empty and this is a no-op; on a restart it restores delivery.
        for handler in self._handlers:
            self.client.on_event(handler)
        await self.client.initialize(config=self.config)
        await self._wait_until_ready()
        await self.client.list_tools()

    @property
    def capabilities(self) -> dict[str, Any]:
        """Capabilities advertised by the service in the initialize handshake."""

        return self.client.capabilities if self.client is not None else {}

    @property
    def supports_config_transition(self) -> bool:
        """Whether the running service negotiated transition support."""

        return self.client is not None and self.client.supports_config_transition

    async def health(self) -> dict[str, Any]:
        return await self._require_client().health()

    async def list_tools(self) -> list[ToolMetadata]:
        return await self._require_client().list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await self._require_client().call_tool(name, arguments)

    async def prepare_config_transition(
        self, next_config: dict[str, Any]
    ) -> ConfigTransitionResult:
        """Prepare a replacement or live-apply ``next_config`` on the child.

        The wrapper retains the successful next config so a required subsequent
        ``stop()`` / ``start()`` replacement initializes the new child with the
        same effective configuration. The dict is retained by reference; it is
        never copied into an error or status envelope.
        """

        transition = await self._require_client().prepare_config_transition(next_config)
        self.config = next_config
        return transition

    def on_event(self, handler: EventHandler) -> None:
        # Record on the wrapper so restarts re-attach it (see start()), and
        # register on the live client if one already exists.
        self._handlers.append(handler)
        if self.client is not None:
            self.client.on_event(handler)

    async def stop(self) -> None:
        process = self.process
        client = self.client
        if client is not None:
            try:
                # Bound the graceful-shutdown RPC: if the child is wedged or no
                # longer reading stdin, an unbounded wait would never reach the
                # terminate/kill fallback below.
                await asyncio.wait_for(client.shutdown(), timeout=3)
            except Exception:
                pass  # wedged/already-closed — fall through to terminate/kill
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
