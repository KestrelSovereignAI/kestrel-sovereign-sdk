"""Host-side client for isolated feature stdio JSON-RPC runtimes."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .protocol import (
    CONFIG_TRANSITION,
    CONFIG_TRANSITION_APPLIED,
    CONFIG_TRANSITION_CAPABILITY,
    FEATURE_EVENT,
    HEALTH,
    HOST_INGRESS,
    HOST_INGRESS_CAPABILITY,
    INITIALIZE,
    PROTOCOL_VERSION,
    SHUTDOWN,
    TOOL_EXECUTION_CONTEXT,
    TOOL_EXECUTION_CONTEXT_CAPABILITY,
    TOOL_EXECUTION_CONTEXT_VERSION,
    TOOLS_CALL,
    TOOLS_LIST,
    ConfigTransitionCapabilities,
    ConfigTransitionError,
    ConfigTransitionResult,
    ConfigTransitionUnsupportedError,
    HostIngressCapabilities,
    HostIngressError,
    HostIngressPayload,
    HostIngressUnknownNameError,
    HostIngressUnsupportedError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    ProtocolError,
    ToolExecutionContext,
    ToolExecutionContextCapabilities,
    ToolExecutionContextUnsupportedError,
    ToolMetadata,
    decode_message,
    encode_message,
    validate_host_ingress_name,
    validate_host_ingress_payload,
)

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]

logger = logging.getLogger(__name__)

# Cap on feature events buffered before any handler subscribes, so a host that
# only uses tools (never calls on_event) can't accumulate events unbounded.
# Oldest events are dropped past this — the buffer exists to cover the brief
# startup window, not to be a durable queue.
_MAX_PENDING_EVENTS = 256
# Every potentially blocking phase of subprocess retirement is bounded.  This
# is deliberately a module constant so regression tests can exercise the
# timeout paths without making the production grace period shorter.
_SUBPROCESS_STOP_TIMEOUT = 3.0


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
        # ``on_event()`` may need a separate buffered-event drain.  Keep every
        # such task owned by this client so retirement cannot discard the
        # client while a handler (and the child-controlled payload it received)
        # is still running.
        self._event_tasks: set[asyncio.Task[None]] = set()
        # Closing streams and joining event delivery is independently owned.
        # A supervisor can time-bound its observation of ``close()`` without
        # cancelling this mandatory cleanup half-way through.
        self._close_task: asyncio.Task[None] | None = None
        # Latched once the read loop terminates (EOF, decode error, cancel).
        # After this is set the stream is dead: request()/start() fail fast with
        # it instead of writing to a broken pipe and awaiting a reply that can
        # never arrive.
        self._closed_exc: BaseException | None = None
        # Notifications that arrive before any handler is registered are buffered
        # and flushed to the first handler. A service may emit feature events
        # (e.g. channel.inbound from an already-linked session) during startup,
        # before the host has called on_event — without this they would be lost.
        self._pending_notifications: deque[dict[str, Any]] = deque(
            maxlen=_MAX_PENDING_EVENTS
        )
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
        if self._closed_exc is not None:
            self._raise_terminal_error()
        if self._read_task is None:
            self._read_task = asyncio.create_task(self._read_loop())

    def on_event(self, handler: EventHandler) -> None:
        # ``close()`` can remain pending in ``writer.wait_closed()`` after it
        # has released event state. Do not let a direct late registration
        # reintroduce a retained handler during that shutdown window.
        if self._shutdown_started:
            return
        first_handler = not self._event_handlers
        self._event_handlers.append(handler)
        if first_handler and self._pending_notifications and not self._shutdown_started:
            self._create_event_task(self._drain_pending())

    def _create_event_task(self, coroutine: Awaitable[None]) -> asyncio.Task[None]:
        """Create one client-owned event-delivery task and retain it to join."""

        task = asyncio.create_task(coroutine)
        self._event_tasks.add(task)
        task.add_done_callback(self._finish_event_task)
        return task

    def _finish_event_task(self, task: asyncio.Task[None]) -> None:
        """Consume a buffered-delivery failure before releasing its ownership.

        A callback is required here rather than a bare ``set.discard``: a
        buffered handler can fail after its client has been detached, and an
        unread task exception both produces asyncio's warning and retains the
        decoded event through its traceback.  Delivery failures are deliberately
        payload-free at this boundary.
        """

        self._event_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:  # pragma: no cover - task raced cancellation
            return
        if exc is not None:
            logger.warning("isolated feature event delivery failed")
            self._discard_exception_traceback(exc)

    @staticmethod
    def _discard_exception_traceback(exc: BaseException) -> None:
        """Best-effort severing of chains that may retain child-controlled data.

        This cleanup sits on a hostile boundary: feature code can raise an
        ``Exception`` subclass which overrides ``__getattribute__`` or
        ``__setattr__`` specifically for the standard exception fields.  Do
        not invoke those overrides here.  Each native BaseException operation
        is isolated so one uncooperative object cannot prevent draining the
        remaining cause/context/ExceptionGroup graph.
        """

        try:
            seen: set[int] = set()
            to_discard: list[BaseException] = [exc]
            while to_discard:
                try:
                    current = to_discard.pop()
                    marker = id(current)
                    if marker in seen:
                        continue
                    seen.add(marker)
                except BaseException:  # noqa: BLE001, S112 -- mandatory cleanup boundary
                    # The worklist only contains BaseExceptions, but cleanup
                    # must remain non-throwing even under pathological runtime
                    # conditions.
                    continue

                # Read every link before clearing it.  Calling the base type's
                # slot directly bypasses a subclass's Python-level attribute
                # hooks; each individual read still gets a guard because some
                # interpreter-provided exception objects can reject mutation.
                try:
                    cause = BaseException.__getattribute__(current, "__cause__")
                except BaseException:  # noqa: BLE001 -- hostile exception attribute
                    cause = None
                try:
                    context = BaseException.__getattribute__(current, "__context__")
                except BaseException:  # noqa: BLE001 -- hostile exception attribute
                    context = None
                if isinstance(current, BaseExceptionGroup):
                    # ``BaseException.__getattribute__`` still honors a
                    # subclass property named ``exceptions``.  Read the
                    # built-in descriptor directly so a hostile
                    # ExceptionGroup subclass cannot hide nested exceptions
                    # whose tracebacks retain decoded feature data.
                    try:
                        nested = BaseExceptionGroup.exceptions.__get__(
                            current, type(current)
                        )
                    except BaseException:  # noqa: BLE001 -- hostile native object
                        nested = None
                else:
                    try:
                        nested = BaseException.__getattribute__(current, "exceptions")
                    except BaseException:  # noqa: BLE001 -- hostile exception attribute
                        nested = None

                for attribute in ("__traceback__", "__cause__", "__context__"):
                    try:
                        BaseException.__setattr__(current, attribute, None)
                    except BaseException:  # noqa: BLE001, S110 -- best-effort scrub
                        pass

                for linked in (cause, context):
                    if isinstance(linked, BaseException):
                        try:
                            to_discard.append(linked)
                        except BaseException:  # noqa: BLE001, S110 -- best-effort scrub
                            pass
                if isinstance(nested, tuple):
                    for member in nested:
                        if isinstance(member, BaseException):
                            try:
                                to_discard.append(member)
                            except BaseException:  # noqa: BLE001, S110 -- best-effort scrub
                                pass
        except BaseException:  # noqa: BLE001 -- sanitization must never escape
            # Sanitization itself is never allowed to strand request futures,
            # terminal state, or a later cleanup phase.
            return

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
            try:
                result = handler(params)
                # EventHandler intentionally accepts every Awaitable, not only
                # a native coroutine.  In particular a handler may return a
                # Task or Future it created itself; awaiting it here keeps that
                # work under the read/drain owner so stop() cannot report
                # success while the child-controlled event remains live in an
                # escaped task.
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError as exc:
                # A separately-created handler Task/Future can be cancelled
                # without cancelling the read task that merely awaited it. In
                # that case it is a handler failure, not reader cancellation.
                # A cancellation actually requested for this owner retains its
                # normal propagation semantics.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                self._discard_exception_traceback(exc)
                raise RuntimeError("isolated feature event delivery failed") from None
            except BaseException as exc:  # noqa: BLE001 -- hostile feature boundary
                self._discard_exception_traceback(exc)
                raise RuntimeError("isolated feature event delivery failed") from None

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

    @property
    def tool_execution_context_capabilities(
        self,
    ) -> ToolExecutionContextCapabilities | None:
        """Return accepted execution-context versions, if advertised by the child.

        A malformed capability is treated as unsupported so a host that requires
        trusted execution metadata fails closed instead of silently dropping it.
        """

        capability = self.capabilities.get(TOOL_EXECUTION_CONTEXT_CAPABILITY)
        if not isinstance(capability, dict):
            return None
        try:
            return ToolExecutionContextCapabilities.from_dict(capability)
        except ProtocolError:
            return None

    @property
    def supports_tool_execution_context(self) -> bool:
        """Whether the initialized service accepts the current context version."""

        capabilities = self.tool_execution_context_capabilities
        return capabilities is not None and capabilities.supports(
            TOOL_EXECUTION_CONTEXT_VERSION
        )

    @property
    def host_ingress_capabilities(self) -> HostIngressCapabilities | None:
        """Return the negotiated private host-ingress contract, if valid.

        Older services do not advertise this capability. Malformed metadata is
        deliberately indistinguishable from an absent capability to callers,
        which makes the typed API fail closed before any ingress request is
        written to a child with unknown behavior.
        """

        capability = self.capabilities.get(HOST_INGRESS_CAPABILITY)
        if not isinstance(capability, dict):
            return None
        try:
            return HostIngressCapabilities.from_dict(capability)
        except ProtocolError:
            return None

    @property
    def supports_host_ingress(self) -> bool:
        """Whether the initialized service advertises a valid ingress contract."""

        return self.host_ingress_capabilities is not None

    def supports_host_ingress_name(self, name: str) -> bool:
        """Whether a valid capability explicitly advertises ``name``.

        Invalid names return ``False`` instead of raising, making this a safe
        probe for host routing code. The invoking API still validates and
        reports a typed failure for invalid names.
        """

        try:
            validated_name = validate_host_ingress_name(name)
        except ProtocolError:
            return False
        capabilities = self.host_ingress_capabilities
        return capabilities is not None and capabilities.supports(validated_name)

    @property
    def replacement_required(self) -> bool:
        """Whether this client has been fenced pending process replacement.

        This is true after a prepare-only transition and after an interrupted
        transition whose remote outcome cannot be known.  Supervisors can use
        it to decide whether a replacement must use the requested next config.
        """

        return self._restart_required

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
                # A ProtocolError can also be the terminal error latched by the
                # reader (for example, a malformed frame from the child).  That
                # is not a normal hook rejection: the transport is unusable and
                # its remote outcome is unknown, so fence it just like EOF or a
                # broken pipe.  Remote JSON-RPC error responses do not close the
                # reader and therefore leave ``_closed_exc`` unset.
                if self._closed_exc is not None:
                    self._mark_restart_required()
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
        # HEALTH is intentionally allowed to run beside normal requests.  If
        # one began before a transition but its reply arrived after the
        # transition fenced this client, its old ``ready`` reply must not revive
        # the local readiness state or hide the replacement requirement.
        if self._restart_required:
            self.ready = False
            return {"status": "restart-required", "ready": False}
        self.ready = result.get("ready") is True or result.get("status") in {
            "ready",
            "ok",
        }
        return result

    async def list_tools(self) -> list[ToolMetadata]:
        if self._restart_required:
            raise ProtocolError(
                "service replacement is required before tools are exposed"
            )
        if not self.ready:
            raise ProtocolError("health must report ready before tools are exposed")
        result = await self.request(TOOLS_LIST)
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            raise ProtocolError("tools/list result must contain a tools list")
        self.tools = [ToolMetadata.from_dict(item) for item in result["tools"]]
        return self.tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        context: ToolExecutionContext | None = None,
    ) -> Any:
        """Call a tool, optionally carrying trusted host execution metadata.

        Context is sent in the reserved ``execution_context`` RPC envelope,
        never merged into the user-controlled ``arguments`` object. A supplied
        context requires explicit child capability advertisement; callers that
        do not supply one retain the legacy wire format and behavior.
        """

        if self._restart_required:
            raise ProtocolError(
                "service replacement is required before tools can be called"
            )
        if not self.ready:
            raise ProtocolError("health must report ready before tools can be called")
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        if context is not None:
            if not isinstance(context, ToolExecutionContext):
                raise TypeError("context must be a ToolExecutionContext")
            capabilities = self.tool_execution_context_capabilities
            if capabilities is None or not capabilities.supports(context.version):
                raise ToolExecutionContextUnsupportedError(
                    "service does not advertise support for this tool execution context"
                )
            params[TOOL_EXECUTION_CONTEXT] = context.to_dict()
        return await self.request(
            TOOLS_CALL,
            params,
        )

    async def call_host_ingress(
        self, name: str, payload: HostIngressPayload = None
    ) -> HostIngressPayload:
        """Invoke one negotiated private host-ingress callback.

        The callback name must have been advertised by the initialized service;
        a legacy, malformed, or unknown capability fails locally and sends no
        request. Payloads are strict, size-bounded JSON values and are checked
        again by the service. All public failures use a generic message so host
        payloads and feature exception text cannot cross this API boundary.
        """

        if self._shutdown_started or self._restart_required:
            raise HostIngressError("host ingress is unavailable")
        try:
            validated_name = validate_host_ingress_name(name)
            validated_payload = validate_host_ingress_payload(payload)
        except ProtocolError as exc:
            raise HostIngressError("host ingress failed") from exc
        capabilities = self.host_ingress_capabilities
        if capabilities is None:
            raise HostIngressUnsupportedError("host ingress is not supported")
        if not capabilities.supports(validated_name):
            raise HostIngressUnknownNameError("host ingress name is not available")
        try:
            result = await self.request(
                HOST_INGRESS,
                {"name": validated_name, "payload": validated_payload},
            )
            return validate_host_ingress_payload(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise HostIngressError("host ingress failed") from exc

    # ``invoke`` reads naturally at host call sites; retain the symmetric alias
    # without adding another wire-level operation.
    invoke_host_ingress = call_host_ingress

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
        """Close streams only after every client-owned event task has settled.

        The actual close runs in a private task.  In particular, a read-task
        done callback is allowed to cancel this caller without being mistaken
        for the read task's expected cancellation: awaiting the private task
        through ``shield`` propagates the caller's exact cancellation rather
        than swallowing it.  The private task remains available to a later
        retirement retry if a cancellation-resistant handler does not settle.
        """

        # Prevent a later public transition call from starting while the stream
        # is being closed by a supervisor.
        self._shutdown_started = True
        task = self._close_task
        if task is None:
            task = asyncio.create_task(self._close_owned())
            self._close_task = task
        return await asyncio.shield(task)

    async def _close_owned(self) -> None:
        """Release streams, then join read and event delivery under one owner."""

        read_task = self._read_task
        tasks = set(self._event_tasks)
        if read_task is not None:
            tasks.add(read_task)
        for task in tasks:
            if not task.done():
                task.cancel()

        # Closing stdin/stdout must not wait for a hostile handler.  The
        # subsequent joins still prove whether it is safe to release this
        # client from retirement ownership.
        self._close_reader_transport()
        close = getattr(self.writer, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001, S110 -- best-effort stream close
                pass

        event_tasks_settled = False
        try:
            try:
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                # The buffer can contain child-provided, potentially secret
                # event dictionaries. Clear it after coordinated event
                # shutdown, before a potentially non-settling writer close.
                # This cannot race a draining task because all owned event and
                # read tasks have already settled.
                if all(task.done() for task in tasks):
                    async with self._event_lock:
                        self._pending_notifications.clear()
                        self._event_handlers.clear()
                    event_tasks_settled = True

            wait_closed = getattr(self.writer, "wait_closed", None)
            if wait_closed is not None:
                await self._wait_writer_closed_owned(wait_closed)
        finally:
            # The buffer can contain child-provided, potentially secret event
            # dictionaries. Event data is released above before writer close;
            # the remaining terminal state waits for the close owner itself.
            if event_tasks_settled:
                if read_task is self._read_task:
                    self._read_task = None
                # Replace the terminal classification with the coordinated-close
                # sentinel.  ``_read_loop`` already discards raw exceptions,
                # but this distinguishes a completed close for later callers.
                self._closed_exc = ConnectionError("isolated feature client is closed")

    async def _wait_writer_closed_owned(
        self, wait_closed: Callable[[], Awaitable[Any]]
    ) -> None:
        """Best-effort writer close without mistaking its cancellation for ours.

        An alternate StreamWriter can await an independently cancelled Future.
        That propagates ``CancelledError`` to this owner without incrementing
        its cancellation count.  Treat it like any other close failure so the
        retained close task settles and a later retirement retry is not fenced
        forever.  A cancellation directed at this owner still propagates.
        """

        owner = asyncio.current_task()
        cancellation_count = owner.cancelling() if owner is not None else 0
        try:
            await wait_closed()
        except asyncio.CancelledError as exc:
            if owner is not None and owner.cancelling() > cancellation_count:
                raise
            self._discard_exception_traceback(exc)
        except BaseException as exc:  # noqa: BLE001 -- best-effort stream close
            # A failed writer close cannot justify retaining decoded event
            # data once all event work has actually stopped.
            self._discard_exception_traceback(exc)

    def _close_reader_transport(self) -> None:
        """Best-effort close of a reader and its underlying pipe transport."""

        close = getattr(self.reader, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001, S110 -- reader compatibility hook
                pass

        feed_eof = getattr(self.reader, "feed_eof", None)
        if feed_eof is not None:
            try:
                feed_eof()
            except Exception:  # noqa: BLE001, S110 -- reader compatibility hook
                pass

        # asyncio.StreamReader exposes its transport privately on supported
        # Python versions.  Some compatible readers expose it publicly instead;
        # checking both keeps shutdown defensive without coupling callers to a
        # platform-specific subprocess implementation.
        transport = getattr(self.reader, "_transport", None)
        if transport is None:
            transport = getattr(self.reader, "transport", None)
        close_transport = getattr(transport, "close", None)
        if close_transport is not None:
            try:
                close_transport()
            except Exception:  # noqa: BLE001, S110 -- reader compatibility hook
                pass

        # An externally retained StreamReader would otherwise keep every byte
        # already pulled from stdout after ownership is retired. This is a
        # defensive release only for readers that expose asyncio's buffer.
        buffer = getattr(self.reader, "_buffer", None)
        clear_buffer = getattr(buffer, "clear", None)
        if clear_buffer is not None:
            try:
                clear_buffer()
            except Exception:  # noqa: BLE001, S110 -- reader compatibility hook
                pass

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        await self.start()
        # Fail fast on a dead stream rather than writing to a broken pipe and
        # awaiting a reply that will never come (the wedge this fixes).
        if self._closed_exc is not None:
            self._raise_terminal_error()
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
            self._raise_terminal_error()
        try:
            self.writer.write(
                encode_message(
                    JsonRpcRequest(method=method, params=params or {}, id=request_id)
                )
            )
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
        raw_exc: BaseException = ConnectionError("isolated feature stream closed")
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
            raw_exc = cancelled
            raise
        except BaseException as loop_exc:  # noqa: BLE001 -- terminal feature boundary
            raw_exc = loop_exc
        finally:
            # Never retain or re-raise the raw reader/handler exception.  Its
            # traceback can hold decoded event parameters, and re-raising it
            # later would attach subsequent request parameters to that same
            # client-owned object.  The stored sentinel preserves only the
            # public terminal classification; every recipient gets a fresh
            # generic instance below.
            try:
                self._closed_exc = self._terminal_sentinel(raw_exc)
            except BaseException:  # noqa: BLE001 -- install generic fallback
                # Even an exotic terminal object must not leave a live stream
                # without a payload-free, generic terminal classification.
                self._closed_exc = ConnectionError("isolated feature stream closed")
            try:
                self._discard_exception_traceback(raw_exc)
            finally:
                # A hostile exception must not prevent every already-issued
                # request from receiving a terminal result.  Clear ownership in
                # a finally block even if an alternate Future implementation
                # rejects inspection or completion.
                try:
                    pending_futures = tuple(self._pending.values())
                except BaseException:  # noqa: BLE001 -- defensive pending snapshot
                    pending_futures = ()
                try:
                    for pending in pending_futures:
                        try:
                            if not pending.done():
                                pending.set_exception(self._fresh_terminal_error())
                        except BaseException as completion_exc:  # noqa: BLE001 -- hostile Future hook
                            # An externally retained Future-compatible object
                            # can fail from either ``done()`` or
                            # ``set_exception()``.  Its traceback includes
                            # this terminal reader frame and may therefore
                            # retain the decoded event that ended the stream.
                            self._discard_exception_traceback(completion_exc)
                            continue
                finally:
                    self._pending.clear()

    def _mark_restart_required(self) -> None:
        """Fence local work after a child restart outcome becomes possible."""

        self._restart_required = True
        self.ready = False

    def _raise_terminal_error(self) -> None:
        """Raise a fresh terminal failure without tainting the stored sentinel."""

        if self._closed_exc is None:  # pragma: no cover - callers guard this helper
            return
        raise self._fresh_terminal_error() from None

    def _terminal_sentinel(self, raw_exc: BaseException) -> BaseException:
        """Return a traceback-free, payload-free terminal classification."""

        if isinstance(raw_exc, EOFError):
            return EOFError("isolated feature stream closed")
        if isinstance(raw_exc, ProtocolError):
            return ProtocolError("isolated feature protocol failed")
        return ConnectionError("isolated feature stream closed")

    def _fresh_terminal_error(self) -> BaseException:
        """Create an unshared generic error for one terminal observation."""

        terminal = self._closed_exc
        if isinstance(terminal, EOFError):
            return EOFError("isolated feature stream closed")
        if isinstance(terminal, ProtocolError):
            return ProtocolError("isolated feature protocol failed")
        if (
            self._shutdown_started
            and self._close_task is not None
            and self._close_task.done()
        ):
            return ConnectionError("isolated feature client is closed")
        return ConnectionError("isolated feature stream closed")

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


@dataclass(frozen=True)
class _PendingConfigTransition:
    """Wrapper-owned config handoff while a transition RPC is in flight."""

    previous_config: dict[str, Any] | None
    next_config: dict[str, Any]


@dataclass
class _ChildRetirement:
    """Private ownership of one detached child until its reaping is known."""

    client: IsolatedFeatureClient | None
    process: asyncio.subprocess.Process | None
    task: asyncio.Task[bool] | None = None
    # ``Process.wait()`` must be started once and observed through shields.
    # Cancelling a fresh coroutine for every bounded phase leaks waiters in the
    # subprocess transport and can retain the child long after stop returned.
    wait_task: asyncio.Task[Any] | None = None
    # Each client phase is started once and retained through an unresolved
    # retirement.  A phase that suppresses cancellation must never make a
    # timeout observer wait for cancellation to finish, nor be forgotten.
    shutdown_task: asyncio.Task[Any] | None = None
    close_task: asyncio.Task[Any] | None = None
    # Completed phase tasks can retain successful child payloads in ``_result``
    # and failure arguments in ``_exception``.  Keep only the outcome needed
    # to preserve one-shot retry semantics once they settle; the task reference
    # itself is reserved for a still-running phase.
    shutdown_attempted: bool = False
    shutdown_settled: bool = False
    shutdown_succeeded: bool = False
    close_attempted: bool = False
    close_settled: bool = False
    close_succeeded: bool = False
    # Signal intent belongs to this exact process generation.  A delayed reap
    # must not turn a stop retry into another TERM/KILL delivery.
    terminate_requested: bool = False
    kill_requested: bool = False
    # Subprocess creation itself is an owned startup phase.  It can suppress
    # cancellation and return a live Process only after public start() timed
    # out, so it needs its own handoff rather than being treated as an ordinary
    # startup task whose result can be discarded.
    spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None
    # Cancellation intent is durable: a hostile task can call uncancel() after
    # observing its first request, but a later stop retry must not send another.
    spawn_cancel_requested: bool = False
    # A startup RPC may suppress cancellation after its own deadline. Keep it
    # with the detached child until it has actually settled, just like close.
    startup_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    startup_cancel_requested: set[asyncio.Task[Any]] = field(default_factory=set)
    # Startup tasks are removed as each one settles, including after a bounded
    # snapshot has returned.  These counters deliberately record no task
    # result or exception object, so an unresolved process reap cannot retain
    # child-controlled startup data indefinitely.
    startup_attempted: int = 0
    startup_settled: int = 0
    startup_succeeded: int = 0


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
    # Serializes wrapper lifecycle/probe RPCs so a health reply cannot span a
    # config transition. ``stop()`` deliberately bypasses this lock and uses
    # ``_state_lock`` plus cancellation instead (see stop()).
    _lifecycle_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    # Guards only the wrapper's small in-memory state transitions.  In
    # particular, it is never held while waiting for a child RPC: ``stop()``
    # must be able to detach and terminate a child whose initialize or health
    # request has wedged.
    _state_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    # Stop calls themselves remain serialized. This avoids a second concurrent
    # stop clearing ``_stopping`` while the first is still terminating its
    # detached child, without making stop wait on a wedged child RPC.
    _stop_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    # Track every wrapper lifecycle/probe operation, including operations
    # queued behind ``_lifecycle_lock``.  ``stop()`` cancels these tasks after
    # atomically detaching the current child, which prevents queued work from
    # reviving or reconfiguring a child the supervisor has retired.
    _active_operations: set[asyncio.Task[Any]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    # Operations cancelled by stop() transfer here before the cancellation is
    # requested.  A task may suppress CancelledError (including by calling
    # uncancel()), so it remains a replacement fence until it really settles
    # rather than becoming invisible when it leaves _active_operations.
    _retiring_operations: set[asyncio.Task[Any]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    # Cancellation is an intent, not a property of Task.cancelling(): a task
    # can clear its own cancellation state.  Retain this per-task marker until
    # it settles so a repeated stop observes rather than re-cancels it.
    _operation_cancel_requested: set[asyncio.Task[Any]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    # One actual lifecycle/config-transition RPC may be in flight because
    # ``_lifecycle_lock`` serializes it with health/start.  Its explicit
    # ownership makes cancellation/stop semantics deterministic: a stop that
    # interrupts it retains its next config before replacement, while a normal
    # hook rejection rolls back to the previous config.
    _pending_transition: _PendingConfigTransition | None = field(
        default=None,
        init=False,
        repr=False,
    )
    # Detached children are not exposed through ``client`` / ``process``, but
    # remain privately owned here until a bounded wait proves they exited.  A
    # failed final reap is intentionally retained so a later stop can retry the
    # *same* process instead of falsely succeeding or spawning a replacement.
    _retirements: list[_ChildRetirement] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    # Public stop callers await this shielded supervisor task.  It is separate
    # from any caller task so cancelling the sole caller cannot cancel or lose
    # the retirement it started.
    _stop_task: asyncio.Task[bool] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    # A stop can win while create_subprocess_exec() is in progress.  The
    # starting task is then cancelled and must settle (having either published
    # or retired its exact child) before stop can truthfully report success.
    _starting: bool = field(default=False, init=False, repr=False)
    # The generation owns the start slot until its ``finally`` has made the
    # child handoff definitive.  Keep uncertainty attached to that same
    # generation so a late timeout result cannot fence a start that already
    # settled while waiting to reacquire ``_state_lock``.
    _starting_generation: int | None = field(default=None, init=False, repr=False)
    _start_uncertain_generation: int | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _start_settled: asyncio.Event = field(
        default_factory=asyncio.Event,
        init=False,
        repr=False,
    )
    # At most one start generation is admitted at a time. Its phase tasks stay
    # here until they finish or are atomically handed to a retirement record.
    _startup_tasks: set[asyncio.Task[Any]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _startup_cancel_requested: set[asyncio.Task[Any]] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    # The process-creation task is tracked separately because, unlike an RPC
    # phase, its late result is a resource that retirement must adopt and reap.
    # It is assigned synchronously before the first await after task creation.
    _spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _spawn_cancel_requested: bool = field(default=False, init=False, repr=False)
    _generation: int = field(default=0, init=False, repr=False)
    _stopping: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        # No start is in flight immediately after construction.
        self._start_settled.set()

    async def start(self) -> None:
        operation = await self._register_operation()
        cancellations: list[tuple[Any, ...]] = []
        failure: BaseException | None = None
        # A new generation is not publicly successful until this outer method
        # returns.  In particular, a caller can be cancelled while the private
        # start finalizer or this operation's unregister handoff is running.
        # Retain the generation so that cancellation still retires *this* child
        # instead of returning an error with a live public handle pair.
        started_generation: int | None = None
        try:
            async with self._lifecycle_lock:
                started_generation = await self._start()
        except BaseException as exc:  # noqa: BLE001 -- preserve cancellation state
            failure = exc
            if isinstance(exc, asyncio.CancelledError):
                cancellations.append(exc.args)
        finally:
            if cancellations or failure is not None:
                try:
                    await self._unregister_operation_owned(operation, cancellations)
                except BaseException as exc:  # noqa: BLE001 -- cleanup must settle
                    if isinstance(exc, asyncio.CancelledError):
                        cancellations.append(exc.args)
                    elif failure is None:
                        failure = exc
            else:
                # ``start()`` has crossed its public success boundary only when
                # this invocation releases its operation. This helper cannot
                # yield, so the event loop cannot run stop() between removal
                # and the return below: stop either already transferred and
                # cancelled this operation (which follows the failure path), or
                # sees no stale long-lived caller Task after success returns.
                self._unregister_successful_start_at_return(operation)
        if started_generation is not None and (cancellations or failure is not None):
            try:
                retired = await self._retire_start_result_owned(
                    started_generation, cancellations
                )
                if failure is None and not retired:
                    failure = RuntimeError("isolated feature retirement is unresolved")
            except BaseException as exc:  # noqa: BLE001 -- cleanup must settle
                if isinstance(exc, asyncio.CancelledError):
                    cancellations.append(exc.args)
                elif failure is None:
                    failure = exc
        if cancellations:
            self._raise_latest_cancellation(cancellations)
        if failure is not None:
            raise failure

    async def _start(self) -> int | None:
        generation: int | None = None
        process: asyncio.subprocess.Process | None = None
        client: IsolatedFeatureClient | None = None
        spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None
        startup_deadline: float | None = None
        retirement_claimed = False
        retirement_task: asyncio.Task[bool] | None = None
        failure: BaseException | None = None
        cancellations: list[tuple[Any, ...]] = []
        try:
            # A published Process can already have exited between callers.  It
            # is not a running client merely because its handle is non-None:
            # detach it into the same authoritative retirement path as stop(),
            # join that exact cleanup under shielded ownership, and only then
            # admit a replacement start.  Recheck beneath the admission lock:
            # the transport can publish returncode while this task is waiting
            # to reacquire it after the retirement helper's first observation.
            while True:
                await self._retire_terminal_published_child(cancellations)
                if cancellations:
                    self._raise_latest_cancellation(cancellations)

                async with self._state_lock:
                    self._clear_settled_start_uncertainty_locked()
                    if self.process is not None or self.client is not None:
                        if not self._published_child_is_terminal_locked():
                            return
                        # The child became terminal in the handoff gap above.
                        # Release the lock and move its exact published pair
                        # through the same authoritative retirement path.
                        continue
                    if (
                        self._stopping
                        or self._starting
                        or self._start_uncertain_generation is not None
                        or self._retirements
                        or self._retiring_operations
                        or (self._stop_task is not None and not self._stop_task.done())
                    ):
                        raise RuntimeError("isolated feature retirement is in progress")
                    # A completed successful supervisor describes the previous child.
                    # Once a new start is admitted, a later stop must create a fresh
                    # attempt for this generation rather than replaying that old success.
                    self._stop_task = None
                    generation = self._generation
                    self._starting = True
                    self._starting_generation = generation
                    self._start_settled.clear()
                    # This deadline starts at generation admission, before
                    # subprocess creation.  A slow spawn is part of startup,
                    # not an unbounded prelude to it.
                    startup_deadline = (
                        asyncio.get_running_loop().time() + self.ready_timeout
                    )
                    break

            # Create and retain subprocess creation before its first await.
            # ``create_subprocess_exec`` can itself be cancellation-resistant
            # on a platform transport.  If it returns a Process after the
            # public start has failed, the exact task is transferred to a
            # retirement record which adopts and reaps that late process.
            spawn_task = asyncio.create_task(
                asyncio.create_subprocess_exec(
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
            )
            self._retain_retirement_task(spawn_task)
            self._spawn_task = spawn_task
            self._spawn_cancel_requested = False
            spawn_task.add_done_callback(self._on_spawn_task_done)
            # ``startup_deadline`` is set synchronously with admission above.
            if startup_deadline is None:  # pragma: no cover - admission is required
                raise RuntimeError("isolated feature startup was not admitted")
            process = await self._await_spawn_phase(spawn_task, startup_deadline)
            # No await occurs between taking the Process result and publishing
            # it.  The event loop therefore cannot expose an unowned process
            # even when another task deliberately holds ``_state_lock``.
            if self._spawn_task is spawn_task:
                self._spawn_task = None
                self._spawn_cancel_requested = False
            spawn_task = None
            if process.stdout is None or process.stdin is None:
                raise RuntimeError("subprocess stdio pipes were not created")
            client = IsolatedFeatureClient(
                process.stdout,
                process.stdin,
                protocol_version=self.protocol_version,
            )
            # Re-attach persisted handlers BEFORE initialize so the inner client's
            # startup-event buffer flushes to them (a relinked service may emit
            # channel.inbound during the handshake). On a first start this list is
            # empty and this is a no-op; on a restart it restores delivery.
            for handler in self._handlers:
                client.on_event(handler)
            # Publishing is deliberately event-loop-local and synchronous.
            # There is no suspension between creation and this assignment, so
            # stop() can never observe a live child in an attachment gap.
            self.process = process
            self.client = client

            # One absolute deadline covers spawn, initialize, health, retry
            # sleep, and tools/list. A child that wedges any phase follows the
            # authoritative retirement path rather than extending startup.
            await self._await_startup_phase(
                client.initialize(config=self.config),
                client,
                process,
                generation,
                startup_deadline,
            )
            await self._wait_until_ready(client, process, generation, startup_deadline)
            await self._await_startup_phase(
                client.list_tools(),
                client,
                process,
                generation,
                startup_deadline,
            )
        except BaseException as exc:  # noqa: BLE001 -- retain startup ownership
            # Do not leave a half-initialized child behind on startup failure or
            # cancellation. If stop() already detached it, its private
            # retirement record is joined here instead of being forgotten.
            failure = exc
            if isinstance(exc, asyncio.CancelledError):
                cancellations.append(exc.args)
            if not retirement_claimed and (
                process is not None or spawn_task is not None
            ):
                retirement_claimed = True
                # Creating the task is synchronous. It therefore owns the
                # exact child *or* spawn task even if a newer cancellation
                # arrives before this task can await the retirement claim.
                if spawn_task is None:
                    retirement_task = asyncio.create_task(
                        self._retire_startup_child(client, process)
                    )
                else:
                    retirement_task = asyncio.create_task(
                        self._retire_startup_child(client, process, spawn_task)
                    )
        finally:
            if retirement_task is not None:
                try:
                    retired = await self._await_owned_task(
                        retirement_task, cancellations
                    )
                    if failure is None and not retired:
                        failure = RuntimeError(
                            "isolated feature retirement is unresolved"
                        )
                except BaseException as exc:  # noqa: BLE001 -- record cleanup failure
                    if isinstance(exc, asyncio.CancelledError):
                        cancellations.append(exc.args)
                    elif failure is None:
                        failure = exc

            if generation is not None:
                # State settlement is separately owned for the same reason as
                # child retirement: a repeated caller cancellation must not
                # strand _starting or a generation fence behind _state_lock.
                finalizer = asyncio.create_task(self._finalize_start(generation))
                try:
                    await self._await_owned_task(finalizer, cancellations)
                except BaseException as exc:  # noqa: BLE001 -- record cleanup failure
                    if isinstance(exc, asyncio.CancelledError):
                        cancellations.append(exc.args)
                    elif failure is None:
                        failure = exc

        # A cancellation that arrives while the mandatory finalizer is queued
        # or running was not visible to the earlier ``except`` block.  The
        # public start call must still either return success or detach this
        # exact child into bounded retirement before it reports that failure.
        if (
            not retirement_claimed
            and generation is not None
            and (cancellations or failure is not None)
            and (client is not None or process is not None)
        ):
            retirement_claimed = True
            retirement_task = asyncio.create_task(
                self._retire_startup_child(client, process)
            )
            try:
                retired = await self._await_owned_task(retirement_task, cancellations)
                if failure is None and not retired:
                    failure = RuntimeError("isolated feature retirement is unresolved")
            except BaseException as exc:  # noqa: BLE001 -- record cleanup failure
                if isinstance(exc, asyncio.CancelledError):
                    cancellations.append(exc.args)
                elif failure is None:
                    failure = exc

        if cancellations:
            self._raise_latest_cancellation(cancellations)
        if failure is not None:
            raise failure
        return generation

    @property
    def capabilities(self) -> dict[str, Any]:
        """Capabilities advertised by the service in the initialize handshake."""

        return self.client.capabilities if self.client is not None else {}

    @property
    def supports_config_transition(self) -> bool:
        """Whether the running service negotiated transition support."""

        return self.client is not None and self.client.supports_config_transition

    @property
    def tool_execution_context_capabilities(
        self,
    ) -> ToolExecutionContextCapabilities | None:
        """Return execution-context versions accepted by the running child."""

        if self.client is None:
            return None
        return self.client.tool_execution_context_capabilities

    @property
    def supports_tool_execution_context(self) -> bool:
        """Whether the running child accepts the current execution context."""

        return self.client is not None and self.client.supports_tool_execution_context

    @property
    def host_ingress_capabilities(self) -> HostIngressCapabilities | None:
        """Return the running child's valid private host-ingress capability."""

        if self.client is None:
            return None
        return self.client.host_ingress_capabilities

    @property
    def supports_host_ingress(self) -> bool:
        """Whether the running child advertises private host ingress."""

        return self.client is not None and self.client.supports_host_ingress

    @property
    def replacement_required(self) -> bool:
        """Whether the running child must be replaced before more work."""

        return self.client is not None and self.client.replacement_required

    async def health(self) -> dict[str, Any]:
        operation = await self._register_operation()
        cancellations: list[tuple[Any, ...]] = []
        failure: BaseException | None = None
        result: dict[str, Any] | None = None
        try:
            async with self._lifecycle_lock:
                client, generation = await self._current_client()
                result = await client.health()
                async with self._state_lock:
                    if generation != self._generation or self.client is not client:
                        raise RuntimeError(
                            "isolated feature was stopped during health check"
                        )
        except BaseException as exc:  # noqa: BLE001 -- preserve cancellation state
            failure = exc
            if isinstance(exc, asyncio.CancelledError):
                cancellations.append(exc.args)
        finally:
            try:
                await self._unregister_operation_owned(operation, cancellations)
            except BaseException as exc:  # noqa: BLE001 -- cleanup must settle
                if isinstance(exc, asyncio.CancelledError):
                    cancellations.append(exc.args)
                elif failure is None:
                    failure = exc
        if cancellations:
            self._raise_latest_cancellation(cancellations)
        if failure is not None:
            raise failure
        if result is None:  # pragma: no cover - client.health() returns an object
            raise RuntimeError("isolated feature health check did not complete")
        return result

    async def list_tools(self) -> list[ToolMetadata]:
        return await self._require_client().list_tools()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        context: ToolExecutionContext | None = None,
    ) -> Any:
        return await self._require_client().call_tool(name, arguments, context=context)

    async def call_host_ingress(
        self, name: str, payload: HostIngressPayload = None
    ) -> HostIngressPayload:
        """Invoke a private host-ingress callback on the running child."""

        return await self._require_client().call_host_ingress(name, payload)

    invoke_host_ingress = call_host_ingress

    async def prepare_config_transition(
        self, next_config: dict[str, Any]
    ) -> ConfigTransitionResult:
        """Prepare a replacement or live-apply ``next_config`` on the child.

        The wrapper owns the config used by its next ``start()``.  It retains
        ``next_config`` on every successful outcome and on an interrupted
        transition that fences the child for replacement; otherwise it restores
        the prior config.  The lifecycle lock prevents a concurrent ``stop()``
        / ``start()`` or health probe from observing an intermediate state. The
        dict is retained by reference and never copied into an error or status
        envelope.
        """

        operation = await self._register_operation()
        cancellations: list[tuple[Any, ...]] = []
        failure: BaseException | None = None
        transition: ConfigTransitionResult | None = None
        try:
            async with self._lifecycle_lock:
                client, generation = await self._current_client()
                previous_config = self.config
                # A second request against an already-fenced client is rejected
                # before it can reach the child.  It must retain the config from
                # the transition that actually caused the fence, not overwrite
                # it with this unrelated later request.
                was_replacement_required = client.replacement_required
                pending: _PendingConfigTransition | None = None
                if not was_replacement_required:
                    pending = _PendingConfigTransition(
                        previous_config=previous_config,
                        next_config=next_config,
                    )
                    async with self._state_lock:
                        if generation != self._generation or self.client is not client:
                            raise RuntimeError(
                                "isolated feature was stopped before transition"
                            )
                        self._pending_transition = pending
                try:
                    transition = await client.prepare_config_transition(next_config)
                except BaseException as exc:  # noqa: BLE001 -- finalize all outcomes
                    # A cancelled local wait cannot retract a request that may
                    # already be on the wire. Only this call's *new* fence earns
                    # ownership of next_config; an existing fence belongs to a
                    # prior transition and leaves previous_config intact.
                    # Hook rejection keeps the old child/config. Terminal
                    # transport failures newly fence this client and therefore
                    # require the next effective config for replacement.
                    failure = exc
                    if isinstance(exc, asyncio.CancelledError):
                        cancellations.append(exc.args)
                    retain_next = (
                        not was_replacement_required and client.replacement_required
                    )
                else:
                    retain_next = True
                # This handoff is mandatory: a second cancellation must not
                # leave both configs reachable through ``_pending_transition``.
                # Give the finalizer private ownership and replay at most the
                # newest cancellation once the state lock update has settled.
                await self._finish_transition_owned(
                    pending, retain_next=retain_next, cancellations=cancellations
                )
        except BaseException as exc:  # noqa: BLE001 -- preserve cancellation state
            if failure is None:
                failure = exc
            if isinstance(exc, asyncio.CancelledError):
                cancellations.append(exc.args)
        finally:
            try:
                await self._unregister_operation_owned(operation, cancellations)
            except BaseException as exc:  # noqa: BLE001 -- cleanup must settle
                if isinstance(exc, asyncio.CancelledError):
                    cancellations.append(exc.args)
                elif failure is None:
                    failure = exc
        if cancellations:
            self._raise_latest_cancellation(cancellations)
        if failure is not None:
            raise failure
        if transition is None:  # pragma: no cover - protocol result is required
            raise RuntimeError("config transition did not complete")
        return transition

    def on_event(self, handler: EventHandler) -> None:
        # Record on the wrapper so restarts re-attach it (see start()), and
        # register on the live client if one already exists.
        self._handlers.append(handler)
        if self.client is not None:
            self.client.on_event(handler)

    async def stop(self) -> None:
        """Retire every owned child, without letting caller cancellation lose it.

        The public handle pair is intentionally detached at the beginning of
        the supervised attempt.  The attempt itself is a private task retained
        by the wrapper, so a cancelled public ``stop()`` keeps waiting (under a
        shield) until the exact child is either reaped or recorded as uncertain.
        Cancellation counts remain intact for enclosing timeout managers; only
        after retirement settles is the newest cancellation delivered to the
        caller without scheduling another one.
        """

        caller = asyncio.current_task()
        async with self._stop_lock:
            task = self._stop_task
            if task is None or (task.done() and not self._stop_task_result(task)):
                task = asyncio.create_task(self._run_stop_attempt(caller))
                self._stop_task = task

        cancellations: list[tuple[Any, ...]] = []
        while True:
            try:
                retired = await asyncio.shield(task)
                break
            except asyncio.CancelledError as cancelled:
                cancellations.append(cancelled.args)

        if cancellations:
            self._replay_cancellations(cancellations)
        if not retired:
            raise RuntimeError("isolated feature retirement is unresolved")

    async def _run_stop_attempt(self, caller: asyncio.Task[Any] | None) -> bool:
        """Detach, cancel competing lifecycle work, and reap known children."""

        try:
            current = asyncio.current_task()
            async with self._state_lock:
                self._stopping = True
                self._clear_settled_start_uncertainty_locked()
                self._generation += 1
                # A transition that has started may already have written its RPC.
                # Preserve the requested config synchronously, before cancelling
                # the task, so a following start cannot observe the old config.
                if self._pending_transition is not None:
                    self.config = self._pending_transition.next_config
                    self._pending_transition = None
                operations = [
                    task
                    for task in self._active_operations
                    # A caller can await start() and then stop() in the same
                    # outer Task. That successful start remains visible until
                    # Task.done() by design, but it has already crossed its
                    # public success boundary and must not cancel its own
                    # stop caller. Every other live operation is transferred.
                    if task is not current and task is not caller and not task.done()
                ]
                for operation in operations:
                    # Transfer before scheduling cancellation.  This is the
                    # only ownership handoff, so an uncancel-suppressing task
                    # can never be lost between the active and retired sets.
                    self._active_operations.discard(operation)
                    self._retiring_operations.add(operation)
                    self._operation_cancel_requested.add(operation)
                    operation.add_done_callback(self._release_retired_operation)
                client = self.client
                process = self.process
                self.client = None
                self.process = None
                starting_generation = self._starting_generation
                startup_tasks = (
                    self._startup_tasks if starting_generation is not None else None
                )
                startup_cancel_requested = (
                    self._startup_cancel_requested
                    if starting_generation is not None
                    else None
                )
                spawn_task = (
                    self._spawn_task if starting_generation is not None else None
                )
                spawn_cancel_requested = self._spawn_cancel_requested
                if spawn_task is not None:
                    self._spawn_task = None
                    self._spawn_cancel_requested = False
                self._claim_retirement_locked(
                    client,
                    process,
                    startup_tasks,
                    spawn_task,
                    startup_cancel_requested,
                    spawn_cancel_requested,
                )
            for operation in operations:
                # The durable marker above deliberately controls retries; do
                # not use Task.cancelling(), which hostile code can reset with
                # uncancel().
                if not operation.done():
                    operation.cancel()

            # When a concurrent start has not yet published its process, do not
            # report a clean stop until that task settles and transfers any child
            # it created into a private retirement record. A bounded wait that
            # does not settle remains an explicit fail-closed uncertainty.
            if (
                starting_generation is not None
                and not await self._wait_for_start_settlement(starting_generation)
            ):
                async with self._state_lock:
                    # The timed wait releases this task before it reacquires
                    # the lock. A cancelled start may have queued its finally
                    # first and settled in that gap, so only fence the exact
                    # generation if it is still demonstrably unsettled now.
                    if not self._start_is_settled_locked(starting_generation):
                        self._start_uncertain_generation = starting_generation
                    else:
                        self._clear_settled_start_uncertainty_locked()
                # The start task can have queued its mandatory private handoff
                # ahead of this timeout observation, yet require one loop turn
                # after the state lock is released to queue its finalizer. Keep
                # the generation fenced and give that exact handoff one more
                # bounded observation before reporting unresolved retirement.
                if not await self._wait_for_start_settlement(starting_generation):
                    return False
                async with self._state_lock:
                    self._clear_settled_start_uncertainty_locked()

            return await self._reap_owned_children()
        except BaseException:  # noqa: BLE001 -- supervisor returns fail-closed status
            # This task is deliberately exception-free: a retained, false result
            # means later stop calls retry exact retirement rather than emitting
            # an unobserved task exception or claiming success.
            return False
        finally:
            async with self._state_lock:
                self._stopping = False

    async def _wait_for_start_settlement(self, generation: int) -> bool:
        """Bound the handoff from one concurrently cancelled start task."""

        try:
            async with asyncio.timeout(_SUBPROCESS_STOP_TIMEOUT):
                await self._start_settled.wait()
        except TimeoutError:
            return False
        async with self._state_lock:
            return self._start_is_settled_locked(generation)

    async def _reap_owned_children(self) -> bool:
        """Join each authoritative retirement and retain any uncertain child."""

        async with self._state_lock:
            # A start may settle after a previous stop timed out.  A later stop
            # is authoritative enough to remove that stale fence once the
            # generation's event and slot show its handoff is complete.
            self._clear_settled_start_uncertainty_locked()
            # An earlier bounded attempt may have reached an uncertain final
            # reap.  A later stop owns a fresh retry of that exact record;
            # reusing its old ``False`` result would merely repeat the false
            # failure without ever observing a delayed process exit.
            for retirement in self._retirements:
                task = retirement.task
                if task is None or (
                    task.done()
                    and (
                        not self._stop_task_result(task)
                        or retirement.startup_tasks
                        or retirement.spawn_task is not None
                    )
                ):
                    task = asyncio.create_task(self._retire_child(retirement))
                    self._retain_retirement_task(task)
                    retirement.task = task
            retirements = list(self._retirements)
        if not retirements:
            async with self._state_lock:
                children_retired = self._start_uncertain_generation is None
            operations_retired = await self._observe_retiring_operations()
            return children_retired and operations_retired

        retired: list[_ChildRetirement] = []
        for retirement in retirements:
            task = retirement.task
            if task is None:
                return False
            try:
                complete = await asyncio.shield(task)
            except BaseException:  # noqa: BLE001 -- one child cannot abort reaping peers
                complete = False
            if complete:
                retired.append(retirement)

        async with self._state_lock:
            for retirement in retired:
                self._remove_retirement_if_complete_locked(retirement)
            # A child that did not settle remains in ``_retirements`` with its
            # exact process handle, fencing replacement and enabling retry.
            self._clear_settled_start_uncertainty_locked()
            children_retired = (
                not self._retirements and self._start_uncertain_generation is None
            )
        operations_retired = await self._observe_retiring_operations()
        return children_retired and operations_retired

    async def _observe_retiring_operations(self) -> bool:
        """Boundedly observe stop-cancelled wrapper operations without recancelling.

        These operations can own ``_lifecycle_lock`` even after the process and
        inner client were retired.  Replacement calls must therefore fail fast
        until they settle; waiting behind that stale lock would bypass a new
        start's own readiness deadline.
        """

        async with self._state_lock:
            self._prune_completed_operations_locked()
            operations = set(self._retiring_operations)
        if not operations:
            return True

        _done, _ = await asyncio.wait(operations, timeout=_SUBPROCESS_STOP_TIMEOUT)
        async with self._state_lock:
            # The operation's own finally only releases its active registration.
            # Retired ownership remains until the outer Task has actually
            # completed; this observer also prunes any callback that has not
            # yet had a chance to acquire the state lock.
            self._prune_completed_operations_locked()
            return not self._retiring_operations

    def _start_is_settled_locked(self, generation: int) -> bool:
        """Return whether ``generation`` has completed its startup handoff."""

        return self._starting_generation != generation and self._start_settled.is_set()

    def _clear_settled_start_uncertainty_locked(self) -> None:
        """Release only an uncertainty whose own start is now settled."""

        generation = self._start_uncertain_generation
        if generation is not None and self._start_is_settled_locked(generation):
            self._start_uncertain_generation = None

    def _remove_retirement_if_complete_locked(
        self, retirement: _ChildRetirement
    ) -> bool:
        """Release a record only after every dynamically attached task settles."""

        if retirement.startup_tasks or retirement.spawn_task is not None:
            return False
        for owned in self._retirements:
            if owned is retirement:
                self._retirements.remove(owned)
                return True
        return False

    async def _finalize_start(self, generation: int) -> None:
        """Settle one admitted start slot under independently owned cleanup."""

        async with self._state_lock:
            # A replacement start cannot normally overlap this one, but retain
            # generation ownership defensively: an old start must never settle
            # the event or clear uncertainty for a newer one.
            if self._starting_generation == generation:
                self._starting = False
                self._starting_generation = None
                # A prior stop can only mark this generation uncertain while it
                # is running. Reaching here means this task has either retired
                # its child or retained an exact record.
                if self._start_uncertain_generation == generation:
                    self._start_uncertain_generation = None
                self._start_settled.set()

    def _claim_retirement_locked(
        self,
        client: IsolatedFeatureClient | None,
        process: asyncio.subprocess.Process | None,
        startup_tasks: set[asyncio.Task[Any]] | None = None,
        spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None,
        startup_cancel_requested: set[asyncio.Task[Any]] | None = None,
        spawn_cancel_requested: bool = False,
    ) -> _ChildRetirement | None:
        """Retain exact detached ownership while ``_state_lock`` is held."""

        if (
            client is None
            and process is None
            and not startup_tasks
            and spawn_task is None
        ):
            return None
        for retirement in self._retirements:
            if (
                (client is not None and retirement.client is client)
                or (process is not None and retirement.process is process)
                or (spawn_task is not None and retirement.spawn_task is spawn_task)
            ):
                if startup_tasks:
                    self._adopt_retired_startup_tasks_locked(retirement, startup_tasks)
                    startup_tasks.clear()
                    if startup_cancel_requested:
                        retirement.startup_cancel_requested.update(
                            startup_cancel_requested
                        )
                        startup_cancel_requested.clear()
                if retirement.spawn_task is None and spawn_task is not None:
                    retirement.spawn_task = spawn_task
                    retirement.spawn_cancel_requested = spawn_cancel_requested
                elif spawn_task is not None:
                    retirement.spawn_cancel_requested = (
                        retirement.spawn_cancel_requested or spawn_cancel_requested
                    )
                return retirement
        retirement = _ChildRetirement(
            client=client,
            process=process,
            spawn_task=spawn_task,
            spawn_cancel_requested=spawn_cancel_requested,
        )
        if startup_tasks:
            self._adopt_retired_startup_tasks_locked(retirement, startup_tasks)
            startup_tasks.clear()
            if startup_cancel_requested:
                retirement.startup_cancel_requested.update(startup_cancel_requested)
                startup_cancel_requested.clear()
        retirement.task = asyncio.create_task(self._retire_child(retirement))
        self._retain_retirement_task(retirement.task)
        self._retirements.append(retirement)
        return retirement

    def _adopt_retired_startup_tasks_locked(
        self,
        retirement: _ChildRetirement,
        tasks: set[asyncio.Task[Any]],
    ) -> None:
        """Transfer startup tasks and arrange autonomous result release.

        The caller holds ``_state_lock`` while moving a live startup set into
        private ownership.  Registering the callback synchronously closes the
        gap after a bounded retirement snapshot: a phase that finishes later
        removes itself even if no caller invokes ``stop()`` again.
        """

        for task in tasks:
            if task in retirement.startup_tasks:
                continue
            retirement.startup_tasks.add(task)
            retirement.startup_attempted += 1
            task.add_done_callback(
                lambda done, retirement=retirement: self._finish_retired_startup_task(
                    retirement, done
                )
            )

    async def _retire_terminal_published_child(
        self, cancellations: list[tuple[Any, ...]]
    ) -> None:
        """Retire a terminal public generation before admitting a replacement.

        A non-``None`` process handle is insufficient proof of liveness: either
        the subprocess transport can publish ``returncode`` or the inner client
        can latch a dead stdout stream while the OS process remains alive. This
        moves that exact pair into the normal private retirement record while
        holding state, then observes its close/reap task through the same
        cancellation-safe owner used by startup-failure cleanup.
        """

        async with self._state_lock:
            if not self._published_child_is_terminal_locked():
                return
            process = self.process
            client = self.client
            self.client = None
            self.process = None
            self._generation += 1
            retirement = self._claim_retirement_locked(client, process)

        if retirement is None:  # pragma: no cover - terminal process is retained
            return
        task = retirement.task
        if task is None:  # pragma: no cover - claim always creates a task
            complete = False
        else:
            complete = await self._await_owned_task(task, cancellations)
        if not complete:
            raise RuntimeError("isolated feature retirement is unresolved")

        async with self._state_lock:
            complete = self._remove_retirement_if_complete_locked(retirement)
        if not complete:
            raise RuntimeError("isolated feature retirement is unresolved")

    def _published_child_is_terminal_locked(self) -> bool:
        """Return whether the currently published pair needs retirement.

        The caller holds ``_state_lock`` so the terminal observation and
        detachment in :meth:`_retire_terminal_published_child` are one atomic
        wrapper state transition.  ``IsolatedFeatureClient`` latches terminal
        stream state synchronously in its read loop before it yields again.
        """

        process = self.process
        client = self.client
        return (process is not None and process.returncode is not None) or (
            client is not None and client._closed_exc is not None
        )

    async def _retire_startup_child(
        self,
        client: IsolatedFeatureClient | None,
        process: asyncio.subprocess.Process | None,
        spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None,
    ) -> bool:
        """Move a failed startup child or spawn task into private ownership."""

        async with self._state_lock:
            if (
                (client is not None or process is not None)
                and self.client is client
                and self.process is process
            ):
                self.client = None
                self.process = None
                self._generation += 1
            if self._spawn_task is spawn_task:
                self._spawn_task = None
                spawn_cancel_requested = self._spawn_cancel_requested
                self._spawn_cancel_requested = False
            else:
                spawn_cancel_requested = False
            retirement = self._claim_retirement_locked(
                client,
                process,
                self._startup_tasks,
                spawn_task,
                self._startup_cancel_requested,
                spawn_cancel_requested,
            )
        if retirement is None:  # pragma: no cover - caller owns a resource/task
            return True

        task = retirement.task
        if task is None:  # pragma: no cover - claim always creates one
            complete = False
        else:
            complete = await asyncio.shield(task)
        if complete:
            async with self._state_lock:
                complete = self._remove_retirement_if_complete_locked(retirement)
        return complete

    async def _retire_start_result_owned(
        self,
        generation: int,
        cancellations: list[tuple[Any, ...]],
    ) -> bool:
        """Retire a successful-but-unreturned start generation exactly once.

        This is the public start success boundary.  ``_start()`` can have
        completed all child RPCs while the outer operation is still unregistering
        itself.  If cancellation or an unregister failure wins in that final
        window, detach only the generation that just completed.  A concurrent
        stop may already have detached it (and advanced ``_generation``); in
        that case its authoritative retirement record remains the owner.
        """

        async with self._state_lock:
            if self._generation != generation:
                return True
            client = self.client
            process = self.process
            if client is None and process is None:
                return True

        # Create the claim task before the first cancellation-sensitive await.
        # It uses the same process/client identity handoff as startup failure,
        # so no later cancellation can expose an unowned published child.
        task = asyncio.create_task(self._retire_startup_child(client, process))
        return await self._await_owned_task(task, cancellations)

    async def _retire_child(self, retirement: _ChildRetirement) -> bool:
        """Run the fully bounded shutdown/TERM/KILL/reap sequence for one child."""

        spawn_retired = await self._retire_spawn_task(retirement)
        startup_retired = await self._retire_startup_tasks(retirement)
        client = retirement.client
        client_retired = spawn_retired and startup_retired
        shutdown_settled = True
        if client is not None:
            shutdown_task = retirement.shutdown_task
            if not retirement.shutdown_attempted:
                shutdown_task = asyncio.create_task(client.shutdown())
                self._retain_retirement_task(shutdown_task)
                retirement.shutdown_task = shutdown_task
                retirement.shutdown_attempted = True
                shutdown_task.add_done_callback(
                    lambda done, retirement=retirement: (
                        self._finish_retired_shutdown_task(retirement, done)
                    )
                )
            if shutdown_task is None:
                shutdown_settled = retirement.shutdown_settled
            else:
                # Give the graceful protocol request its own bounded observation
                # before stream disposal begins. A hostile child cannot block the
                # TERM/KILL fallback, but it can no longer preempt a graceful
                # shutdown that would otherwise succeed.
                shutdown_settled, _ = await self._observe_retirement_task(shutdown_task)
                if shutdown_settled:
                    self._finish_retired_shutdown_task(retirement, shutdown_task)
            # A completed task retains its result or exception arguments. The
            # record callback reduces that to booleans, and a still-live task
            # is reloaded below if close() can settle it.
            shutdown_task = None

        process = retirement.process
        process_retired = process is None or await self._wait_for_process(retirement)
        if process is not None and not process_retired:
            terminate_sent = False
            if process.returncode is None and not retirement.terminate_requested:
                # On Windows the Proactor owns an outstanding stdout pipe read
                # until the child exits. Closing that reader transport first can
                # invalidate its handle while the subprocess transport is still
                # observing it. Signal, then observe that exact waiter before
                # disposing streams; the bounded wait remains the authoritative
                # reap proof.
                retirement.terminate_requested = True
                terminate_sent = True
                try:
                    process.terminate()
                except (ProcessLookupError, OSError):
                    pass
            # A Process-compatible implementation may publish returncode from
            # terminate() before its retained wait task has joined the
            # subprocess transport. The post-signal observation is still
            # required before a real reader transport can be closed.
            if terminate_sent:
                process_retired = await self._wait_for_process(retirement)

            kill_sent = False
            if (
                not process_retired
                and process.returncode is None
                and not retirement.kill_requested
            ):
                retirement.kill_requested = True
                kill_sent = True
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    pass
            if not process_retired and kill_sent:
                process_retired = await self._wait_for_process(retirement)

        close_started_this_attempt = False
        if client is not None:
            close_task = retirement.close_task
            if not retirement.close_attempted:
                close_task = asyncio.create_task(client.close())
                self._retain_retirement_task(close_task)
                retirement.close_task = close_task
                retirement.close_attempted = True
                close_started_this_attempt = True
                close_task.add_done_callback(
                    lambda done, retirement=retirement: self._finish_retired_close_task(
                        retirement, done
                    )
                )

            if close_task is None:
                close_settled = retirement.close_settled
                close_succeeded = retirement.close_succeeded
            else:
                close_settled, close_succeeded = await self._observe_retirement_task(
                    close_task
                )
                if close_settled:
                    self._finish_retired_close_task(retirement, close_task)
                    close_succeeded = retirement.close_succeeded
            # No later phase needs the close task itself. Avoid retaining
            # child-controlled results or exception arguments through the
            # final bounded reap below.
            close_task = None
            # close() can terminate the reader and thereby settle a shutdown
            # request that had just exhausted its first bounded observation.
            # Re-observe only an already-complete task here: this same stop
            # attempt should release a fully settled child, but must not add an
            # unbounded (or duplicate full-timeout) shutdown wait.
            if not shutdown_settled:
                shutdown_task = retirement.shutdown_task
                if shutdown_task is not None and shutdown_task.done():
                    self._finish_retired_shutdown_task(retirement, shutdown_task)
                shutdown_settled = retirement.shutdown_settled
                shutdown_task = None
            # Event delivery may be cancellation-resistant.  It retains
            # child-controlled payloads and must remain authoritative until
            # both phases are settled and close has completed successfully,
            # even if the OS process is already dead. Releasing this client
            # here would admit a restart while a prior generation task lives.
            client_retired = (
                spawn_retired
                and startup_retired
                and shutdown_settled
                and close_settled
                and close_succeeded
            )
            if client_retired:
                async with self._state_lock:
                    if retirement.startup_tasks:
                        client_retired = False
                    else:
                        retirement.client = None
            # Starting client stream disposal can release a subprocess transport
            # waiter. That earns one final bounded process observation in this
            # attempt only; a later retry cannot gain another window merely by
            # re-observing an already-complete close task.

        if process is None:
            return client_retired
        if process_retired:
            return client_retired
        if close_started_this_attempt:
            # This check follows close only because stream disposal can unblock
            # the retained waiter. It deliberately does not run for a
            # process-only retirement or a later close-task retry: TERM and
            # KILL already received their authoritative bounded observations.
            return client_retired and await self._wait_for_process(retirement)
        # Every signal has already received its own bounded reap observation.
        # If the retained waiter still has not settled, preserve this exact
        # record for a retry without adding a fourth timeout window.
        return False

    async def _retire_spawn_task(self, retirement: _ChildRetirement) -> bool:
        """Cancel/observe one owned spawn and adopt a late Process exactly once."""

        spawn_task = retirement.spawn_task
        if spawn_task is None:
            return True
        self._cancel_retired_spawn_once(retirement, spawn_task)
        settled, succeeded = await self._observe_retirement_task(spawn_task)
        if not settled:
            return False

        process: asyncio.subprocess.Process | None = None
        if succeeded:
            try:
                process = spawn_task.result()
            except BaseException as exc:  # noqa: BLE001 -- observed above
                # A failed spawn has no Process to retire. The task's done
                # callback already clears the original traceback.
                IsolatedFeatureClient._discard_exception_traceback(exc)

        async with self._state_lock:
            # Another private observer can only have processed this task after
            # it settled. Do not overwrite a process it already adopted.
            if retirement.spawn_task is not spawn_task:
                return retirement.process is not None or process is None
            retirement.spawn_task = None
            if process is not None and retirement.process is None:
                retirement.process = process
                stdout = getattr(process, "stdout", None)
                stdin = getattr(process, "stdin", None)
                if stdout is not None and stdin is not None:
                    # This child never reached public publication, but its
                    # stdio still needs the same stream disposal as a normal
                    # client before it is reaped.
                    retirement.client = IsolatedFeatureClient(
                        stdout,
                        stdin,
                        protocol_version=self.protocol_version,
                    )
        return True

    async def _retire_startup_tasks(self, retirement: _ChildRetirement) -> bool:
        """Bound and retain startup phases that survived their own deadline."""

        tasks = set(retirement.startup_tasks)
        if not tasks:
            return True
        for task in tasks:
            self._cancel_retired_startup_task_once(retirement, task)
        done, _ = await asyncio.wait(tasks, timeout=_SUBPROCESS_STOP_TIMEOUT)
        for task in done:
            self._finish_retired_startup_task(retirement, task)
        # A phase task can be attached while this bounded observation yields.
        # The done callback above removes only its exact completed task, so a
        # late attachment remains authoritative and a late completion releases
        # itself without requiring another stop retry.
        return not retirement.startup_tasks

    @staticmethod
    def _completed_retirement_task_succeeded(task: asyncio.Task[Any]) -> bool:
        """Consume one completed phase outcome without retaining its payload."""

        if task.cancelled():
            return False
        try:
            task.result()
        except BaseException as exc:  # noqa: BLE001 -- cleanup boundary
            IsolatedFeatureClient._discard_exception_traceback(exc)
            return False
        return True

    def _finish_retired_shutdown_task(
        self,
        retirement: _ChildRetirement,
        task: asyncio.Task[Any],
    ) -> None:
        """Replace one completed shutdown task with its sanitized outcome."""

        if retirement.shutdown_task is not task:
            return
        retirement.shutdown_succeeded = self._completed_retirement_task_succeeded(task)
        retirement.shutdown_settled = True
        retirement.shutdown_task = None

    def _finish_retired_close_task(
        self,
        retirement: _ChildRetirement,
        task: asyncio.Task[Any],
    ) -> None:
        """Replace one completed close task with its sanitized outcome."""

        if retirement.close_task is not task:
            return
        retirement.close_succeeded = self._completed_retirement_task_succeeded(task)
        retirement.close_settled = True
        retirement.close_task = None

    def _finish_retired_startup_task(
        self,
        retirement: _ChildRetirement,
        task: asyncio.Task[Any],
    ) -> None:
        """Release one settled startup task while preserving only its outcome."""

        if task not in retirement.startup_tasks:
            return
        retirement.startup_succeeded += int(
            self._completed_retirement_task_succeeded(task)
        )
        retirement.startup_settled += 1
        retirement.startup_tasks.discard(task)
        retirement.startup_cancel_requested.discard(task)

    def _on_spawn_task_done(
        self, spawn_task: asyncio.Task[asyncio.subprocess.Process]
    ) -> None:
        """Continue private retirement when a cancellation-suppressing spawn ends."""

        # A normally completing live start consumes the result synchronously
        # before the event loop can run this callback. A transferred task is
        # instead retained by a private record and must be reaped even if no
        # caller makes another stop() attempt.
        if self._spawn_task is spawn_task:
            return
        for retirement in self._retirements:
            if retirement.spawn_task is spawn_task:
                task = asyncio.create_task(
                    self._complete_late_spawn_retirement(retirement, spawn_task)
                )
                self._retain_retirement_task(task)
                return

    async def _complete_late_spawn_retirement(
        self,
        retirement: _ChildRetirement,
        spawn_task: asyncio.Task[asyncio.subprocess.Process],
    ) -> None:
        """Retry and release one late-spawn retirement without public help."""

        async with self._state_lock:
            if (
                retirement not in self._retirements
                or retirement.spawn_task is not spawn_task
            ):
                return
            task = retirement.task
            if task is None or task.done():
                task = asyncio.create_task(self._retire_child(retirement))
                self._retain_retirement_task(task)
                retirement.task = task
        try:
            complete = await asyncio.shield(task)
        except BaseException:  # noqa: BLE001 -- preserve the private fence
            complete = False
        if complete:
            async with self._state_lock:
                self._remove_retirement_if_complete_locked(retirement)

    async def _wait_for_process(self, retirement: _ChildRetirement) -> bool:
        """Observe one retained process waiter without cancelling it on timeout."""

        process = retirement.process
        if process is None:
            return True
        wait_task = retirement.wait_task
        if wait_task is None:
            # Even a published returncode still needs its one wait coroutine
            # observed.  That joins the subprocess transport and proves the
            # prior generation has completed cleanup before a replacement is
            # admitted, rather than treating a merely terminal handle as done.
            wait_task = asyncio.create_task(process.wait())
            self._retain_retirement_task(wait_task)
            retirement.wait_task = wait_task
        settled, succeeded = await self._observe_retirement_task(wait_task)
        if not settled:
            return False
        if not succeeded:
            # A Process-compatible implementation can publish returncode just
            # before its wait task reports an error. Retain it unless that
            # definitive terminal state is visible.
            return process.returncode is not None
        try:
            result = wait_task.result()
        except BaseException as exc:  # pragma: no cover - observed above # noqa: BLE001
            IsolatedFeatureClient._discard_exception_traceback(exc)
            return process.returncode is not None
        # asyncio's Process.wait() returns the reaped integer return code.
        # Keep the explicit check as a fail-closed guard for alternate
        # Process-compatible implementations that return None prematurely.
        return process.returncode is not None or result is not None

    @staticmethod
    def _retain_retirement_task(task: asyncio.Task[Any]) -> None:
        """Consume a late phase failure even if no caller retries stop()."""

        def consume(done: asyncio.Task[Any]) -> None:
            if done.cancelled():
                return
            try:
                exc = done.exception()
            except asyncio.CancelledError:  # pragma: no cover - race-safe guard
                return
            if exc is not None:
                IsolatedFeatureClient._discard_exception_traceback(exc)

        task.add_done_callback(consume)

    def _cancel_spawn_once(
        self, task: asyncio.Task[asyncio.subprocess.Process]
    ) -> None:
        """Request one spawn cancellation across live and retired ownership."""

        if task.done():
            return
        if self._spawn_task is task:
            if self._spawn_cancel_requested:
                return
            self._spawn_cancel_requested = True
            task.cancel()
            return
        for retirement in self._retirements:
            if retirement.spawn_task is task:
                self._cancel_retired_spawn_once(retirement, task)
                return

    @staticmethod
    def _cancel_retired_spawn_once(
        retirement: _ChildRetirement,
        task: asyncio.Task[asyncio.subprocess.Process],
    ) -> None:
        """Request exactly one cancellation for a spawn in a retirement record."""

        if task.done() or retirement.spawn_cancel_requested:
            return
        retirement.spawn_cancel_requested = True
        task.cancel()

    def _cancel_startup_task_once(self, task: asyncio.Task[Any]) -> None:
        """Request one phase cancellation across live and retired ownership."""

        if task.done():
            return
        if task in self._startup_tasks:
            if task in self._startup_cancel_requested:
                return
            self._startup_cancel_requested.add(task)
            task.cancel()
            return
        for retirement in self._retirements:
            if task in retirement.startup_tasks:
                self._cancel_retired_startup_task_once(retirement, task)
                return

    @staticmethod
    def _cancel_retired_startup_task_once(
        retirement: _ChildRetirement,
        task: asyncio.Task[Any],
    ) -> None:
        """Request exactly one cancellation for one retained startup phase."""

        if task.done() or task in retirement.startup_cancel_requested:
            return
        retirement.startup_cancel_requested.add(task)
        task.cancel()

    @staticmethod
    async def _observe_retirement_task(
        task: asyncio.Task[Any],
    ) -> tuple[bool, bool]:
        """Hard-bound observation without cancelling or discarding ``task``.

        ``asyncio.wait_for`` waits for a cancelled coroutine to finish. Using
        ``asyncio.wait`` leaves the owned phase task running after the deadline,
        so the supervisor can continue process retirement and a later stop can
        observe that same task rather than creating another one.
        """

        done, _ = await asyncio.wait((task,), timeout=_SUBPROCESS_STOP_TIMEOUT)
        if not done:
            return False, False
        if task.cancelled():
            return True, False
        try:
            task.result()
        except BaseException:  # noqa: BLE001 -- task callback consumed details
            return True, False
        return True, True

    @staticmethod
    def _stop_task_result(task: asyncio.Task[bool]) -> bool:
        """Read a supervisor result defensively; it is never allowed to escape."""

        if task.cancelled():
            return False
        try:
            return task.result()
        except BaseException:  # noqa: BLE001 -- supervisor results are fail-closed
            return False

    @staticmethod
    async def _await_owned_task(
        task: asyncio.Task[Any], cancellations: list[tuple[Any, ...]]
    ) -> Any:
        """Join private work while retaining, but not redelivering, cancellation."""

        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError as cancelled:
                # Catching CancelledError does not decrement Task.cancelling().
                # Continuing to await is therefore safe until another cancel
                # arrives, and preserving that count lets nested timeout
                # managers distinguish their own expiry from external cancel.
                cancellations.append(cancelled.args)

    @staticmethod
    def _replay_cancellations(cancellations: list[tuple[Any, ...]]) -> None:
        """Deliver the newest observed cancellation without scheduling another."""

        raise asyncio.CancelledError(*cancellations[-1])

    _raise_latest_cancellation = _replay_cancellations

    async def _await_spawn_phase(
        self,
        task: asyncio.Task[asyncio.subprocess.Process],
        deadline: float,
    ) -> asyncio.subprocess.Process:
        """Observe the already-owned subprocess spawn within startup's deadline."""

        try:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            done, _ = await asyncio.wait((task,), timeout=remaining)
            if not done:
                self._cancel_spawn_once(task)
                raise TimeoutError
            try:
                return task.result()
            except asyncio.CancelledError as phase_cancelled:
                # ``task.result()`` can raise because the owned spawn was
                # independently cancelled.  That is a failed startup phase,
                # not cancellation of this public start observer.
                IsolatedFeatureClient._discard_exception_traceback(phase_cancelled)
                raise RuntimeError(
                    "isolated feature startup phase was cancelled"
                ) from None
        except asyncio.CancelledError:
            self._cancel_spawn_once(task)
            raise

    async def _await_startup_phase(
        self,
        awaitable: Awaitable[Any],
        client: IsolatedFeatureClient,
        process: asyncio.subprocess.Process,
        generation: int,
        deadline: float,
    ) -> Any:
        """Run one startup phase to an absolute deadline without losing it.

        ``asyncio.timeout_at`` has the same cancellation-waiting behavior as
        ``wait_for`` when its inner coroutine catches ``CancelledError``. A
        separately owned task lets this start fail promptly while retirement
        keeps observing the exact phase task until it actually settles.
        """

        task = asyncio.create_task(awaitable)
        self._retain_retirement_task(task)
        # Task ownership is event-loop-local and therefore synchronous: no
        # cancellation or competing stop() task can run between create_task()
        # and this insertion. Stop transfers this exact set while holding its
        # state lock; the done callback only removes it from the still-live
        # set, never from a retirement record it has already entered.
        self._startup_tasks.add(task)
        task.add_done_callback(self._finish_startup_task)

        try:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            done, _ = await asyncio.wait((task,), timeout=remaining)
            if not done:
                self._cancel_startup_task_once(task)
                raise TimeoutError
            try:
                return task.result()
            except asyncio.CancelledError as phase_cancelled:
                # See _await_spawn_phase(): cancellation of this child task is
                # a sanitized generic startup failure.  Only cancellation of
                # the observing start task propagates as CancelledError.
                IsolatedFeatureClient._discard_exception_traceback(phase_cancelled)
                raise RuntimeError(
                    "isolated feature startup phase was cancelled"
                ) from None
        except asyncio.CancelledError:
            self._cancel_startup_task_once(task)
            raise

    def _finish_startup_task(self, task: asyncio.Task[Any]) -> None:
        """Synchronously release a completed phase from the live handoff set."""

        self._startup_tasks.discard(task)
        self._startup_cancel_requested.discard(task)

    async def _wait_until_ready(
        self,
        client: IsolatedFeatureClient,
        process: asyncio.subprocess.Process,
        generation: int,
        deadline: float,
    ) -> None:
        """Wait for readiness without extending the startup deadline."""

        while True:
            health = await self._await_startup_phase(
                client.health(), client, process, generation, deadline
            )
            if health.get("ready") is True or health.get("status") in {"ready", "ok"}:
                return
            await self._await_startup_phase(
                asyncio.sleep(0.1), client, process, generation, deadline
            )

    def _require_client(self) -> IsolatedFeatureClient:
        if self.client is None:
            raise RuntimeError("isolated feature client is not started")
        return self.client

    async def _current_client(self) -> tuple[IsolatedFeatureClient, int]:
        """Snapshot the current child without holding state across its RPC."""

        async with self._state_lock:
            if self.client is None:
                raise RuntimeError("isolated feature client is not started")
            if self._stopping:
                raise RuntimeError("isolated feature stop is in progress")
            return self.client, self._generation

    async def _register_operation(self) -> asyncio.Task[Any]:
        """Make this lifecycle/probe task cancellable by ``stop()``."""

        operation = asyncio.current_task()
        if operation is None:  # pragma: no cover - asyncio always provides one
            raise RuntimeError("isolated feature operation has no asyncio task")
        async with self._state_lock:
            # A done callback normally releases transferred ownership.  Prune
            # here too so callback scheduling can never leave a completed task
            # as a stale replacement fence or cause unbounded retention.
            self._prune_completed_operations_locked()
            if (
                self._stopping
                or self._retiring_operations
                or self._retirements
                or self._start_uncertain_generation is not None
            ):
                raise RuntimeError("isolated feature retirement is in progress")
            self._active_operations.add(operation)
        return operation

    async def _unregister_operation(self, operation: asyncio.Task[Any]) -> None:
        async with self._state_lock:
            self._active_operations.discard(operation)
            # stop() can transfer this outer task before its finally runs.
            # Removing a transferred task from this child cleanup coroutine
            # creates a one-turn admission gap while the operation itself is
            # still running.  Its done callback releases retired ownership
            # only after ``Task.done()`` becomes true.
            if operation not in self._retiring_operations:
                self._operation_cancel_requested.discard(operation)

    def _unregister_successful_start_at_return(
        self, operation: asyncio.Task[Any]
    ) -> None:
        """Synchronously end one successful ``start()`` invocation's ownership.

        This deliberately does not acquire ``_state_lock``: acquiring an
        ``asyncio.Lock`` could suspend after removing the operation and recreate
        the old post-unregister race. Asyncio only switches tasks at suspension
        points, and this method has none, so these two mutations and the public
        return form one event-loop-local boundary. A concurrent ``stop()`` that
        ran earlier has already moved the operation to
        ``_retiring_operations`` and cancelled it; the outer start then takes
        its cancellation/failure retirement path instead of reaching here.
        """

        self._active_operations.discard(operation)
        self._operation_cancel_requested.discard(operation)

    def _release_retired_operation(self, operation: asyncio.Task[Any]) -> None:
        """Autonomously drop a transferred operation after its outer task ends."""

        self._consume_retired_operation_failure(operation)
        task = asyncio.create_task(self._release_retired_operation_owned(operation))
        self._retain_retirement_task(task)

    async def _release_retired_operation_owned(
        self, operation: asyncio.Task[Any]
    ) -> None:
        """Release exactly one settled transferred operation under state ownership."""

        async with self._state_lock:
            if operation.done():
                self._retiring_operations.discard(operation)
                self._operation_cancel_requested.discard(operation)

    def _prune_completed_operations_locked(self) -> None:
        """Release completed operation ownership while ``_state_lock`` is held."""

        completed = {
            operation
            for operation in self._active_operations | self._retiring_operations
            if operation.done()
        }
        for operation in completed:
            self._consume_retired_operation_failure(operation)
        self._active_operations.difference_update(completed)
        self._retiring_operations.difference_update(completed)
        self._operation_cancel_requested.difference_update(completed)

    @staticmethod
    def _consume_retired_operation_failure(operation: asyncio.Task[Any]) -> None:
        """Mark a transferred operation exception observed without changing await.

        Stop owns an operation after it requests cancellation, but a task may
        suppress that cancellation and later fail from its generation fence. A
        done callback/prune path must retrieve that failure before dropping the
        final task reference; otherwise asyncio reports ``Task exception was
        never retrieved`` and retains its traceback. Calling ``exception()``
        only marks the task's stored result observed, so a caller that still
        holds the task can later await the same result or exception normally.
        """

        if operation.cancelled():
            return
        try:
            exc = operation.exception()
        except asyncio.CancelledError:
            # A task can transition to cancelled between the check above and
            # retrieval. Expected cancellation is not an operation failure.
            return
        except BaseException as retrieval_failure:  # noqa: BLE001 -- Task API guard
            IsolatedFeatureClient._discard_exception_traceback(retrieval_failure)
            return
        if exc is not None:
            IsolatedFeatureClient._discard_exception_traceback(exc)

    async def _unregister_operation_owned(
        self,
        operation: asyncio.Task[Any],
        cancellations: list[tuple[Any, ...]],
    ) -> None:
        """Unregister an operation even if its caller is cancelled repeatedly."""

        task = asyncio.create_task(self._unregister_operation(operation))
        await self._await_owned_task(task, cancellations)

    async def _finish_transition_owned(
        self,
        pending: _PendingConfigTransition | None,
        *,
        retain_next: bool,
        cancellations: list[tuple[Any, ...]],
    ) -> None:
        """Finalize one config handoff under private, shielded ownership."""

        if pending is None:
            return
        task = asyncio.create_task(
            self._finish_transition(pending, retain_next=retain_next)
        )
        await self._await_owned_task(task, cancellations)

    async def _finish_transition(
        self,
        pending: _PendingConfigTransition | None,
        *,
        retain_next: bool,
    ) -> None:
        """Finish only the transition that still owns wrapper config state."""

        if pending is None:
            return
        async with self._state_lock:
            if self._pending_transition is pending:
                self.config = (
                    pending.next_config if retain_next else pending.previous_config
                )
                self._pending_transition = None


def _dict_field(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ProtocolError(f"{key} must be an object")
    return value
