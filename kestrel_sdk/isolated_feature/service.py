"""Service-side base class for isolated feature runtimes."""

from __future__ import annotations

import asyncio
import os
import queue
import sys
import threading
import weakref
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import partial
from inspect import isawaitable, iscoroutinefunction
from typing import Any, BinaryIO

from .context import _active_tool_execution_context, _ToolExecutionContextScope
from .protocol import (
    CONFIG_TRANSITION,
    CONFIG_TRANSITION_APPLIED,
    CONFIG_TRANSITION_CAPABILITY,
    CONFIG_TRANSITION_RESTART,
    FEATURE_EVENT,
    HEALTH,
    HOST_INGRESS,
    HOST_INGRESS_CAPABILITY,
    INBOUND_PRODUCER_CAPABILITY,
    INITIALIZE,
    PROTOCOL_VERSION,
    SHUTDOWN,
    TOOL_EXECUTION_CONTEXT,
    TOOL_EXECUTION_CONTEXT_CAPABILITY,
    TOOLS_CALL,
    TOOLS_LIST,
    ConfigTransitionCapabilities,
    ConfigTransitionResult,
    HostIngressCapabilities,
    HostIngressPayload,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    ProtocolError,
    ToolExecutionContext,
    ToolExecutionContextCapabilities,
    ToolMetadata,
    decode_message,
    encode_message,
    validate_host_ingress_name,
    validate_host_ingress_payload,
)

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any] | Any]
HostIngressHandler = Callable[
    [HostIngressPayload], Awaitable[HostIngressPayload] | HostIngressPayload
]


class _ThreadedStdioReader:
    """Async line reader for Windows inherited standard-input handles.

    ``asyncio.create_subprocess_exec(..., stdin=PIPE, stdout=PIPE)`` gives a
    Windows child regular inherited anonymous-pipe handles. They are valid for
    synchronous file I/O, but are not guaranteed to be overlapped handles, so
    Proactor ``connect_read_pipe`` rejects them with ``WinError 6``.

    The one daemon worker is deliberately not asyncio's default executor. A
    cancelled ``readline`` cannot interrupt a synchronous pipe read, and
    ``asyncio.run`` waits for default-executor workers during teardown. Keeping
    this owned worker daemonized means cancellation and process exit remain
    bounded even if the parent leaves stdin open forever.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._requests: queue.Queue[
            tuple[
                weakref.ReferenceType[asyncio.AbstractEventLoop],
                weakref.ReferenceType[asyncio.Future[bytes]],
            ]
            | None
        ] = queue.Queue()
        self._state_lock = threading.Lock()
        self._pending: dict[asyncio.Future[bytes], asyncio.AbstractEventLoop] = {}
        self._thread: threading.Thread | None = None
        self._closed = False

    async def readline(self) -> bytes:
        loop = asyncio.get_running_loop()
        completion: asyncio.Future[bytes] = loop.create_future()
        completion.add_done_callback(
            partial(_forget_reader_completion, weakref.ref(self))
        )

        with self._state_lock:
            if self._closed:
                return b""
            self._pending[completion] = loop
            self._requests.put((weakref.ref(loop), weakref.ref(completion)))
            if self._thread is None:
                self._thread = threading.Thread(
                    target=_threaded_stdio_read_loop,
                    args=(self._stream, self._requests),
                    name="isolated-feature-stdio-reader",
                    daemon=True,
                )
                self._thread.start()

        # Do not let caller cancellation cancel the owned read completion. The
        # worker remains its sole producer and can safely finish after this task
        # has gone away.
        return await asyncio.shield(completion)

    def close(self) -> None:
        """Stop accepting reads without closing a stream under an active read."""

        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(self._pending.items())
            self._pending.clear()
            while True:
                try:
                    self._requests.get_nowait()
                except queue.Empty:
                    break
            self._requests.put(None)
        for completion, loop in pending:
            self._resolve_result(loop, completion, b"")

    def _forget_completion(self, completion: asyncio.Future[bytes]) -> None:
        with self._state_lock:
            self._pending.pop(completion, None)

    @staticmethod
    def _resolve_result(
        loop: asyncio.AbstractEventLoop,
        completion: asyncio.Future[bytes],
        result: bytes,
    ) -> None:
        try:
            loop.call_soon_threadsafe(_set_future_result, completion, result)
        except RuntimeError:
            # A daemon worker may finish only after asyncio.run() has closed
            # its loop. There is then no remaining reader to notify.
            return

    @staticmethod
    def _resolve_exception(
        loop: asyncio.AbstractEventLoop,
        completion: asyncio.Future[bytes],
        error: BaseException,
    ) -> None:
        try:
            loop.call_soon_threadsafe(_set_future_exception, completion, error)
        except RuntimeError:
            return


class _ThreadedStdioWriter:
    """StreamWriter-compatible serialized output for Windows inherited stdout.

    Every drain owns a queued frame and its completion future. The daemon worker
    is the only code that writes or closes ``stream``. In particular, a caller
    cancelled while awaiting ``drain`` cannot release the service write lock and
    let a later worker overlap or overtake its still-running write.

    A write or flush error is terminal: even if the synchronous stream accepted
    some bytes first, the JSON-RPC line framing can no longer be trusted. The
    worker fences all queued drains under ``_state_lock`` before it reports the
    error, so a concurrent sender either joins that failed set or observes the
    terminal state before it can enqueue another frame.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._pending = bytearray()
        self._state_lock = threading.Lock()
        self._jobs: queue.Queue[
            tuple[
                bytes,
                asyncio.AbstractEventLoop | None,
                asyncio.Future[None] | None,
            ]
            | None
        ] = queue.Queue()
        self._drains: dict[asyncio.Future[None], asyncio.AbstractEventLoop] = {}
        self._closing = False
        # A terminal wire is a classification, not an exception cache: a caught
        # stream error can contain sensitive payloads and traceback references.
        self._terminal_failure = False
        self._thread = threading.Thread(
            target=self._write_loop,
            name="isolated-feature-stdio-writer",
            daemon=True,
        )
        self._thread.start()

    def write(self, data: bytes) -> None:
        with self._state_lock:
            if self._terminal_failure:
                raise _stdio_writer_terminal_error()
            if self._closing:
                raise ConnectionError("isolated feature stdio writer is closed")
            self._pending.extend(data)

    async def drain(self) -> None:
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._terminal_failure:
                raise _stdio_writer_terminal_error()
            if self._closing:
                raise ConnectionError("isolated feature stdio writer is closed")
            data = bytes(self._pending)
            self._pending.clear()
            if not data:
                return
            completion: asyncio.Future[None] = loop.create_future()
            completion.add_done_callback(_consume_future_exception)
            self._drains[completion] = loop
            self._jobs.put((data, loop, completion))

        # Shield the worker-owned completion from cancellation. A cancelled
        # sender may stop awaiting it, but the queued frame remains ordered and
        # the next drain is queued strictly behind it.
        await asyncio.shield(completion)

    def close(self) -> None:
        """Queue a nonblocking close after every owned frame has finished."""

        with self._state_lock:
            if self._terminal_failure:
                return
            if self._closing:
                return
            self._closing = True
            data = bytes(self._pending)
            self._pending.clear()
            if data:
                # A StreamWriter caller normally drains after each write, but
                # close must not silently discard an already accepted frame.
                self._jobs.put((data, None, None))
            self._jobs.put(None)

    def _write_loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                break
            data, loop, completion = job
            try:
                self._write_and_flush(data)
            except BaseException:  # noqa: BLE001 -- report I/O failures
                failed_drains = self._latch_terminal_failure()
                for failed_completion, failed_loop in failed_drains:
                    _set_future_exception_threadsafe(
                        failed_loop,
                        failed_completion,
                        _stdio_writer_terminal_error(),
                    )
                # The failed job can contain a partially framed RPC payload.
                # Do not retain it while the worker closes the stream.
                job = None
                data = b""
                loop = None
                completion = None
                failed_drains = ()
                failed_completion = None
                failed_loop = None
                break
            else:
                if loop is not None and completion is not None:
                    with self._state_lock:
                        self._drains.pop(completion, None)
                    _set_future_result_threadsafe(loop, completion)
                # Queue.get() can block forever while the writer remains open.
                # Do not let this worker frame pin a successfully completed
                # drain or its payload for that idle lifetime.
                job = None
                data = b""
                loop = None
                completion = None
        try:
            self._stream.close()
        except BaseException:  # noqa: BLE001 -- daemon teardown has no retry owner
            # Closing is a best-effort teardown operation. There is no waiting
            # caller at this point, and propagating from a daemon thread is only
            # noisy; the stream has no safer owner left to retry it.
            return

    def _latch_terminal_failure(
        self,
    ) -> tuple[tuple[asyncio.Future[None], asyncio.AbstractEventLoop], ...]:
        """Fence queued frames and return every drain that must fail."""

        with self._state_lock:
            self._terminal_failure = True
            self._pending.clear()
            failed_drains = tuple(self._drains.items())
            self._drains.clear()
            while True:
                try:
                    self._jobs.get_nowait()
                except queue.Empty:
                    break
            return failed_drains

    def _write_and_flush(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = self._stream.write(view)
            if written is None:
                raise BlockingIOError("isolated feature stdio writer would block")
            if written == 0:
                raise BrokenPipeError("isolated feature stdio writer closed")
            view = view[written:]
        self._stream.flush()


def _set_future_result(future: asyncio.Future[Any], result: Any) -> None:
    if not future.done():
        future.set_result(result)


def _set_future_exception(future: asyncio.Future[Any], error: BaseException) -> None:
    if not future.done():
        future.set_exception(error)


def _set_future_result_threadsafe(
    loop: asyncio.AbstractEventLoop, completion: asyncio.Future[None]
) -> None:
    try:
        loop.call_soon_threadsafe(_set_future_result, completion, None)
    except RuntimeError:
        return


def _set_future_exception_threadsafe(
    loop: asyncio.AbstractEventLoop,
    completion: asyncio.Future[None],
    error: BaseException,
) -> None:
    try:
        loop.call_soon_threadsafe(_set_future_exception, completion, error)
    except RuntimeError:
        return


def _consume_future_exception(completion: asyncio.Future[Any]) -> None:
    """Mark an abandoned worker failure observed without changing await semantics."""

    if completion.cancelled():
        return
    completion.exception()


def _stdio_writer_terminal_error() -> ConnectionError:
    """Return a fresh, payload-free error for a permanently unusable wire."""

    return ConnectionError("isolated feature stdio writer has a terminal I/O failure")


def _forget_reader_completion(
    reader_ref: weakref.ReferenceType[_ThreadedStdioReader],
    completion: asyncio.Future[bytes],
) -> None:
    """Drop a finished read without a future callback retaining its reader."""

    reader = reader_ref()
    if reader is not None:
        reader._forget_completion(completion)


def _threaded_stdio_read_loop(
    stream: BinaryIO,
    requests: queue.Queue[
        tuple[
            weakref.ReferenceType[asyncio.AbstractEventLoop],
            weakref.ReferenceType[asyncio.Future[bytes]],
        ]
        | None
    ],
) -> None:
    """Read in a daemon without retaining a reader, loop, or completion."""

    while (request := requests.get()) is not None:
        loop_ref, completion_ref = request
        try:
            # Keep only weak refs while this potentially permanent read blocks.
            line = stream.readline()
        except BaseException as error:  # noqa: BLE001 -- surface stream failures
            _resolve_weak_reader_exception(loop_ref, completion_ref, error)
        else:
            _resolve_weak_reader_result(loop_ref, completion_ref, line)
        line = b""


def _resolve_weak_reader_result(
    loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop],
    completion_ref: weakref.ReferenceType[asyncio.Future[bytes]],
    result: bytes,
) -> None:
    loop = loop_ref()
    completion = completion_ref()
    if loop is not None and completion is not None:
        _ThreadedStdioReader._resolve_result(loop, completion, result)


def _resolve_weak_reader_exception(
    loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop],
    completion_ref: weakref.ReferenceType[asyncio.Future[bytes]],
    error: BaseException,
) -> None:
    loop = loop_ref()
    completion = completion_ref()
    if loop is not None and completion is not None:
        _ThreadedStdioReader._resolve_exception(loop, completion, error)


def _open_private_wire() -> BinaryIO:
    """Duplicate stdout for RPC framing without leaking it into descendants."""

    wire_fd = os.dup(1)
    try:
        # ``os.dup`` is inheritable on Windows. The wire is a private service
        # resource, never an input for a feature-launched descendant.
        os.set_inheritable(wire_fd, False)
        return os.fdopen(wire_fd, "wb", buffering=0)
    except BaseException:
        with suppress(OSError):
            os.close(wire_fd)
        raise


def _is_async_callable(handler: ToolHandler) -> bool:
    """True if calling ``handler`` returns a coroutine to await.

    Covers plain ``async def`` functions, ``functools.partial`` around them
    (``iscoroutinefunction`` unwraps partials), and callable instances whose
    ``__call__`` is ``async def``. Everything else is treated as sync and
    offloaded to a thread in :meth:`IsolatedFeatureService.call_tool`.
    """

    if iscoroutinefunction(handler):
        return True
    return callable(handler) and iscoroutinefunction(handler.__call__)


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
        # Private host callbacks are separate from tools so registering one can
        # never make it agent-discoverable. They use the same async/sync worker
        # model as tools, but accept only validated JSON payloads.
        self._host_ingress_handlers: dict[str, HostIngressHandler] = {}
        self._host_ingress_handler_is_async: dict[str, bool] = {}
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
        self._has_inbound_producer: bool | None = None
        self._inbound_producer_declaration_frozen = False
        # Unset by default. A service must explicitly opt in before the host may
        # send the config lifecycle request.
        self._config_transition_capabilities: ConfigTransitionCapabilities | None = None
        # A successful prepare-only hook has retired old resources. Refuse new
        # tool calls until the host performs the required replacement.
        self._restart_required = False
        # New SDK services understand the fixed, validated execution-context
        # envelope. Legacy services simply lack this initialize capability, so
        # current hosts can fail closed before sending context to them.
        self._tool_execution_context_capabilities = ToolExecutionContextCapabilities()

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

    def advertise_inbound_producer(self, has_producer: bool) -> None:
        """Declare whether this service owns an unmanaged inbound producer.

        A producer polls, listens, or otherwise receives work independently of
        host tool calls or private host ingress. Hosts use this exact boolean
        declaration when deciding whether an idle child can be retired safely.
        Services that do not call this method remain deliberately ambiguous so
        current hosts fail resident rather than silently dropping inbound work.
        Call this before ``initialize``. When ownership derives from host
        config, call it from :meth:`configure`; that hook runs inside the
        initialize handshake before capabilities are assembled. The negotiated
        value is frozen after a successful handshake. A config transition that
        would change ownership must require child restart and re-negotiation.
        """

        if type(has_producer) is not bool:
            raise TypeError("has_producer must be a bool")
        if (
            self._inbound_producer_declaration_frozen
            and has_producer is not self._has_inbound_producer
        ):
            raise RuntimeError(
                "inbound producer declaration is negotiated at initialize"
            )
        self._has_inbound_producer = has_producer

    def advertise_config_transition(self, *, supports_live_apply: bool = False) -> None:
        """Opt into host config-transition lifecycle requests.

        Override :meth:`on_config_transition` to retire old resources before a
        replacement, then return :meth:`ConfigTransitionResult.restart_required`.
        A service that atomically switches resources in-process can set
        ``supports_live_apply=True`` and return
        :meth:`ConfigTransitionResult.applied`. The opt-in keeps legacy services
        and hosts compatible with their existing restart behavior.
        """

        self._config_transition_capabilities = ConfigTransitionCapabilities(
            supports_live_apply=supports_live_apply
        )

    def register_tool(self, metadata: ToolMetadata, handler: ToolHandler) -> None:
        """Register one callable tool and its advertised metadata."""

        self._tools[metadata.name] = metadata
        self._handlers[metadata.name] = handler
        self._handler_is_async[metadata.name] = _is_async_callable(handler)

    def register_host_ingress(self, name: str, handler: HostIngressHandler) -> None:
        """Register one private host-to-service ingress callback.

        Each registration advertises its name through the versioned
        ``host_ingress`` initialize capability. This is deliberately distinct
        from :meth:`register_tool`: it does not alter ``tools/list`` or any
        agent tool inventory. Register callbacks before ``initialize`` so the
        host observes the complete negotiated capability.
        """

        validated_name = validate_host_ingress_name(name)
        if not callable(handler):
            raise TypeError("host ingress handler must be callable")
        self._host_ingress_handlers[validated_name] = handler
        self._host_ingress_handler_is_async[validated_name] = _is_async_callable(
            handler
        )

    # Keep the longer spelling available for hosts/services that use an
    # explicit "handler" noun in their registration APIs.
    register_host_ingress_handler = register_host_ingress

    async def get_tools(self) -> list[ToolMetadata]:
        """Return tools exposed by this service."""

        return list(self._tools.values())

    async def health(self) -> dict[str, Any]:
        """Return service readiness. Override to include feature-specific checks."""

        if self._restart_required:
            return {"status": "restart-required", "ready": False}
        return {"status": "ready", "ready": True}

    async def on_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if requested != self.protocol_version:
            raise ProtocolError(
                "unsupported protocolVersion "
                f"{requested!r}; expected {self.protocol_version!r}"
            )
        config = params.get("config")
        if isinstance(config, dict):
            self.host_config = config
            await self.configure(config)
        capabilities: dict[str, Any] = {"tools": True, "events": True}
        if self._channel_capability is not None:
            capabilities["channel"] = dict(self._channel_capability)
        if self._has_inbound_producer is not None:
            capabilities[INBOUND_PRODUCER_CAPABILITY] = self._has_inbound_producer
        if self._config_transition_capabilities is not None:
            capabilities[CONFIG_TRANSITION_CAPABILITY] = (
                self._config_transition_capabilities.to_dict()
            )
        if self._tool_execution_context_capabilities is not None:
            capabilities[TOOL_EXECUTION_CONTEXT_CAPABILITY] = (
                self._tool_execution_context_capabilities.to_dict()
            )
        if self._host_ingress_handlers:
            capabilities[HOST_INGRESS_CAPABILITY] = HostIngressCapabilities(
                names=tuple(self._host_ingress_handlers)
            ).to_dict()
        result = {
            "protocolVersion": self.protocol_version,
            "serverInfo": {"name": self.name, "version": self.version},
            "capabilities": capabilities,
        }
        self._inbound_producer_declaration_frozen = True
        return result

    async def configure(self, config: dict[str, Any]) -> None:
        """Apply host-provided configuration from the initialize handshake.

        Override to consume persisted/UI feature config that the host forwards
        (the service is launched as a bare process, so this handshake is the
        only path for non-environment configuration). Default is a no-op.
        """

    async def on_config_transition(
        self, next_config: dict[str, Any]
    ) -> ConfigTransitionResult:
        """Prepare or live-apply a host config change.

        This hook runs while :attr:`host_config` and all service resources still
        describe the old effective configuration. It receives the next config
        separately so an implementation can retire an old webhook with old
        credentials before returning ``restart_required``. The default is a
        successful prepare-only result; services must first call
        :meth:`advertise_config_transition` for the host to invoke it.

        To live apply, atomically switch resources to ``next_config`` and return
        :meth:`ConfigTransitionResult.applied`. The SDK changes
        :attr:`host_config` only after that successful result. Do not log config
        values or include them in exceptions.
        """

        return ConfigTransitionResult.restart_required()

    async def on_shutdown(self) -> dict[str, Any]:
        self._stopping = True
        return {"ok": True}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Dispatch a tool call. Override for custom routing."""

        if self._restart_required:
            raise ProtocolError("service is awaiting restart")
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

    async def call_host_ingress(
        self, name: str, payload: HostIngressPayload
    ) -> HostIngressPayload:
        """Dispatch a private host ingress callback without exposing a tool.

        Native coroutine handlers run on the event loop; synchronous handlers
        run in ``asyncio.to_thread`` so a blocked host callback cannot starve
        health checks or unrelated concurrent RPCs.
        """

        if self._stopping or self._restart_required:
            raise ProtocolError("host ingress is unavailable")
        handler = self._host_ingress_handlers.get(name)
        if handler is None:
            raise ProtocolError("host ingress is unavailable")
        if self._host_ingress_handler_is_async.get(name):
            result = await handler(payload)
        else:
            result = await asyncio.to_thread(handler, payload)
            if isawaitable(result):
                result = await result
        # The result crosses the same JSON-RPC boundary. Validate it before
        # framing so a handler cannot emit an unbounded/non-JSON response.
        return validate_host_ingress_payload(result)

    # Symmetric naming for code that uses "invoke" on the client side.
    invoke_host_ingress = call_host_ingress

    async def emit_event(
        self, event_type: str, payload: dict[str, Any] | None = None
    ) -> None:
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
                    if message.method in (INITIALIZE, CONFIG_TRANSITION, SHUTDOWN):
                        # Handle inline (not concurrently):
                        #  * INITIALIZE must finish the handshake + apply host
                        #    config before any later request runs, or a pipelined
                        #    health/tools-call could hit an uninitialized service;
                        #  * CONFIG_TRANSITION sees old config, finishes cleanup,
                        #    and replies before a queued shutdown can begin;
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

        # Claim a PRIVATE duplicate of the real stdout fd for the JSON-RPC wire,
        # then repoint the process's stdout (fd 1 and sys.stdout) at stderr.
        # After this, a stray print(), C-extension banner, or dependency
        # deprecation notice written to stdout lands on the inherited stderr as
        # a harmless log line instead of corrupting protocol framing and killing
        # the connection. The protocol owns a fd nothing else can reach.
        wire = _open_private_wire()
        writer: Any | None = None
        reader: Any | None = None
        try:
            os.dup2(2, 1)
            sys.stdout = sys.stderr
            if sys.platform == "win32":
                # See _ThreadedStdioReader: inherited child pipe handles are
                # not Proactor-ready. The duplicate remains private to the RPC
                # wire, and is closed below after the service has stopped.
                reader = _ThreadedStdioReader(sys.stdin.buffer)
                writer = _ThreadedStdioWriter(wire)
            else:
                reader = asyncio.StreamReader()
                loop = asyncio.get_running_loop()
                protocol = asyncio.StreamReaderProtocol(reader)
                await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
                write_transport, write_protocol = await loop.connect_write_pipe(
                    asyncio.streams.FlowControlMixin,
                    wire,
                )
                writer = asyncio.StreamWriter(
                    write_transport, write_protocol, reader, loop
                )
            await self.serve(reader, writer)
        finally:
            # ``wait_closed`` is intentionally not awaited: a parent that has
            # stopped consuming output must not make child shutdown unbounded.
            # Closing the private duplicate releases the descriptor in either
            # branch, including a setup failure before a StreamWriter exists.
            if isinstance(reader, _ThreadedStdioReader):
                reader.close()
            close = getattr(writer, "close", None)
            try:
                if close is not None:
                    close()
            finally:
                # The Windows writer owns ``wire`` until its daemon worker has
                # flushed every queued frame and closed it. POSIX transports do
                # not use that worker and retain the original eager cleanup.
                if not isinstance(writer, _ThreadedStdioWriter) and not wire.closed:
                    wire.close()

    async def _handle_request(self, request: JsonRpcRequest) -> None:
        try:
            result = await self._dispatch(request)
            await self._send(JsonRpcResponse(id=request.id, result=result))
        except ProtocolError as exc:
            await self._send(
                JsonRpcResponse(
                    id=request.id,
                    error=JsonRpcError(
                        code=-32602,
                        message=self._error_message(request, exc),
                    ),
                )
            )
        except asyncio.CancelledError:
            # A host callback can await a child task that was independently
            # cancelled. That cancellation is a callback failure, not a
            # cancellation of this RPC task, and must still receive a bounded
            # response. Preserve cancellation actually directed at this request
            # task so serve() can stop in-flight work during shutdown.
            current = asyncio.current_task()
            if request.method != HOST_INGRESS or (
                current is not None and current.cancelling()
            ):
                raise
            await self._send(
                JsonRpcResponse(
                    id=request.id,
                    error=JsonRpcError(code=-32602, message="host ingress failed"),
                )
            )
        except Exception as exc:  # noqa: BLE001 -- serialize handler failures
            await self._send(
                JsonRpcResponse(
                    id=request.id,
                    error=JsonRpcError(
                        code=-32603,
                        message=self._error_message(request, exc),
                    ),
                )
            )

    async def _dispatch(self, request: JsonRpcRequest) -> Any:
        if request.method == INITIALIZE:
            return await self.on_initialize(request.params)
        if request.method == CONFIG_TRANSITION:
            if self._restart_required:
                raise ProtocolError("service is awaiting restart")
            if self._config_transition_capabilities is None:
                raise ProtocolError("config transitions are not supported")
            next_config = request.params.get("config")
            if not isinstance(next_config, dict):
                raise ProtocolError("config transition requires config")
            result = await self.on_config_transition(next_config)
            if not isinstance(result, ConfigTransitionResult):
                raise ProtocolError("config transition hook returned an invalid result")
            if (
                result.action == CONFIG_TRANSITION_APPLIED
                and not self._config_transition_capabilities.supports_live_apply
            ):
                raise ProtocolError(
                    "config transition hook returned an unadvertised action"
                )
            if result.action == CONFIG_TRANSITION_RESTART:
                self._restart_required = True
            else:
                # The hook owns the atomic resource switch. Keep the old config
                # available throughout it and commit the next one only after it
                # reports a successful live apply.
                self.host_config = next_config
            return result.to_dict()
        if request.method == HEALTH:
            # Keep restart fencing outside the overridable health() hook. A
            # feature-specific readiness implementation must not accidentally
            # report ready after it has retired resources for replacement.
            if self._restart_required:
                return {"status": "restart-required", "ready": False}
            return await self.health()
        if request.method == TOOLS_LIST:
            # Feature packages may intentionally override get_tools() and
            # call_tool(); enforce the lifecycle boundary before either hook.
            if self._restart_required:
                raise ProtocolError("service is awaiting restart")
            return {"tools": [tool.to_dict() for tool in await self.get_tools()]}
        if request.method == TOOLS_CALL:
            if self._restart_required:
                raise ProtocolError("service is awaiting restart")
            name = request.params.get("name")
            if not isinstance(name, str) or not name:
                raise ProtocolError("tools/call requires name")
            arguments = request.params.get("arguments", {})
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise ProtocolError("tools/call arguments must be an object")
            context: ToolExecutionContext | None = None
            if TOOL_EXECUTION_CONTEXT in request.params:
                if self._tool_execution_context_capabilities is None:
                    raise ProtocolError("tool execution context is not supported")
                context = ToolExecutionContext.from_dict(
                    request.params[TOOL_EXECUTION_CONTEXT]
                )
                if not self._tool_execution_context_capabilities.supports(
                    context.version
                ):
                    raise ProtocolError(
                        "tool execution context version is not supported"
                    )
            # ContextVar state is task-local, but its values are copied into
            # child tasks and ``asyncio.to_thread`` workers. Store a shared
            # scope rather than the immutable context directly and revoke it
            # before the reset, so copied contexts cannot retain invocation
            # metadata after this RPC succeeds, fails, or is cancelled.
            scope = _ToolExecutionContextScope(context)
            token = _active_tool_execution_context.set(scope)
            try:
                return await self.call_tool(name, arguments)
            finally:
                scope.invalidate()
                _active_tool_execution_context.reset(token)
        if request.method == HOST_INGRESS:
            # Keep lifecycle fencing in dispatch, not only in the overridable
            # hook, so a subclass cannot accidentally accept ingress after it
            # has shut down or requested process replacement. Likewise, check
            # registration before entering the overridable hook so a raw RPC
            # cannot bypass the capability registry.
            if self._stopping or self._restart_required:
                raise ProtocolError("host ingress is unavailable")
            if set(request.params) != {"name", "payload"}:
                raise ProtocolError("host ingress request is invalid")
            name = validate_host_ingress_name(request.params.get("name"))
            if name not in self._host_ingress_handlers:
                raise ProtocolError("host ingress is unavailable")
            payload = validate_host_ingress_payload(request.params.get("payload"))
            result = await self.call_host_ingress(name, payload)
            return validate_host_ingress_payload(result)
        if request.method == SHUTDOWN:
            # Latch termination before calling the overridable cleanup hook.
            # Overrides may omit ``super()`` or raise, but neither may leave
            # ingress open or cause ``serve()`` to keep reading requests.
            self._stopping = True
            return await self.on_shutdown()
        raise ProtocolError(f"unknown method: {request.method}")

    @staticmethod
    def _error_message(request: JsonRpcRequest, error: Exception) -> str:
        """Return an RPC-safe error without reflecting host config values.

        The service does not log JSON-RPC params. Generic lifecycle envelopes
        also prevent a feature exception that interpolates a token or whole
        config dict from crossing the transport boundary.
        """

        if request.method == CONFIG_TRANSITION:
            return "config transition failed"
        if request.method == HOST_INGRESS:
            return "host ingress failed"
        if request.method == INITIALIZE and isinstance(
            request.params.get("config"), dict
        ):
            return "initialization failed"
        return str(error)

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
