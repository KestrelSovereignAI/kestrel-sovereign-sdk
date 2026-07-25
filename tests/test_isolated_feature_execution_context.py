"""Execution-context propagation and isolation for isolated tool RPCs."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import asyncio
import threading

import pytest

from kestrel_sdk.isolated_feature import (
    TOOL_EXECUTION_CONTEXT,
    TOOLS_CALL,
    IsolatedFeatureClient,
    IsolatedFeatureService,
    JsonRpcRequest,
    ProtocolError,
    ToolExecutionContext,
    ToolExecutionContextUnsupportedError,
    ToolExecutionTrigger,
    ToolMetadata,
    get_tool_execution_context,
)

from .test_isolated_feature import memory_stdio_pair


_INPUT_SCHEMA = {"type": "object", "properties": {}}


def _context(*, attempt: int = 1, invocation_id: str = "occurrence-42") -> ToolExecutionContext:
    return ToolExecutionContext(
        invocation_id=invocation_id,
        idempotency_key="durable-effect-42",
        attempt=attempt,
        trigger=ToolExecutionTrigger(
            kind="scheduler",
            id="occurrence-42",
            source_id="daily-report",
            triggered_at=datetime(2026, 7, 25, 15, 30, tzinfo=timezone.utc),
            scheduled_for=datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc),
        ),
    )


async def _ready_client(
    service: IsolatedFeatureService,
) -> tuple[IsolatedFeatureClient, asyncio.Task[None]]:
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)
    await client.initialize()
    await client.health()
    return client, service_task


async def _close_client(
    client: IsolatedFeatureClient, service_task: asyncio.Task[None]
) -> None:
    try:
        if not service_task.done():
            await client.shutdown()
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_execution_context_round_trip_does_not_mutate_tool_arguments():
    service = IsolatedFeatureService(name="fake", version="1.0.0")
    seen: list[tuple[dict[str, object], ToolExecutionContext | None]] = []

    async def handler(arguments):
        seen.append((arguments, get_tool_execution_context()))
        context = get_tool_execution_context()
        assert context is not None
        return {"invocation_id": context.invocation_id, "attempt": context.attempt}

    service.register_tool(ToolMetadata(name="run", description="", input_schema=_INPUT_SCHEMA), handler)
    client, service_task = await _ready_client(service)
    context = _context()
    arguments = {"execution_context": "ordinary-user-tool-input", "payload": "hello"}

    try:
        assert client.supports_tool_execution_context
        assert await client.call_tool("run", arguments, context=context) == {
            "invocation_id": "occurrence-42",
            "attempt": 1,
        }
        assert seen == [(arguments, context)]
    finally:
        await _close_client(client, service_task)


@pytest.mark.asyncio
async def test_retry_preserves_idempotency_key_and_changes_attempt():
    service = IsolatedFeatureService(name="fake", version="1.0.0")
    observed: list[ToolExecutionContext] = []

    async def handler(arguments):
        context = get_tool_execution_context()
        assert context is not None
        observed.append(context)
        return {"ok": True}

    service.register_tool(ToolMetadata(name="effect", description="", input_schema=_INPUT_SCHEMA), handler)
    client, service_task = await _ready_client(service)
    first = _context(attempt=1)
    second = replace(first, attempt=2)

    try:
        await client.call_tool("effect", context=first)
        await client.call_tool("effect", context=second)
        assert [item.idempotency_key for item in observed] == [
            "durable-effect-42",
            "durable-effect-42",
        ]
        assert [item.attempt for item in observed] == [1, 2]
    finally:
        await _close_client(client, service_task)


@pytest.mark.asyncio
async def test_concurrent_calls_cannot_observe_each_others_context():
    service = IsolatedFeatureService(name="fake", version="1.0.0")
    entered = asyncio.Event()
    release = asyncio.Event()
    observed: dict[str, list[str]] = {"one": [], "two": []}

    async def handler(arguments):
        slot = arguments["slot"]
        context = get_tool_execution_context()
        assert context is not None
        observed[slot].append(context.invocation_id)
        if len(observed["one"]) and len(observed["two"]):
            entered.set()
        await release.wait()
        after_wait = get_tool_execution_context()
        assert after_wait is not None
        observed[slot].append(after_wait.invocation_id)
        return {"slot": slot}

    service.register_tool(ToolMetadata(name="run", description="", input_schema=_INPUT_SCHEMA), handler)
    client, service_task = await _ready_client(service)

    try:
        first = asyncio.create_task(
            client.call_tool("run", {"slot": "one"}, context=_context(invocation_id="one"))
        )
        second = asyncio.create_task(
            client.call_tool("run", {"slot": "two"}, context=_context(invocation_id="two"))
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        release.set()
        assert await first == {"slot": "one"}
        assert await second == {"slot": "two"}
        assert observed == {"one": ["one", "one"], "two": ["two", "two"]}
    finally:
        release.set()
        await _close_client(client, service_task)


@pytest.mark.asyncio
async def test_async_and_sync_handlers_receive_the_same_execution_context():
    service = IsolatedFeatureService(name="fake", version="1.0.0")

    async def async_handler(arguments):
        context = get_tool_execution_context()
        assert context is not None
        return context.to_dict()

    def sync_handler(arguments):
        context = get_tool_execution_context()
        assert context is not None
        return context.to_dict()

    service.register_tool(ToolMetadata(name="async", description="", input_schema=_INPUT_SCHEMA), async_handler)
    service.register_tool(ToolMetadata(name="sync", description="", input_schema=_INPUT_SCHEMA), sync_handler)
    client, service_task = await _ready_client(service)
    context = _context()

    try:
        assert await client.call_tool("async", context=context) == context.to_dict()
        assert await client.call_tool("sync", context=context) == context.to_dict()
    finally:
        await _close_client(client, service_task)


@pytest.mark.asyncio
async def test_failure_and_cancelled_wait_leave_no_execution_context_behind():
    class RecordingService(IsolatedFeatureService):
        def __init__(self):
            super().__init__(name="fake", version="1.0.0")
            self.context_while_sending: list[ToolExecutionContext | None] = []

        async def _send(self, message):
            self.context_while_sending.append(get_tool_execution_context())
            await super()._send(message)

    service = RecordingService()
    started = asyncio.Event()
    release = asyncio.Event()
    seen_without_context: list[ToolExecutionContext | None] = []

    async def failing(arguments):
        assert get_tool_execution_context() == _context()
        raise RuntimeError("expected failure")

    async def delayed(arguments):
        assert get_tool_execution_context() == _context(invocation_id="cancelled")
        started.set()
        await release.wait()
        return {"done": True}

    async def no_context(arguments):
        seen_without_context.append(get_tool_execution_context())
        return {"ok": True}

    service.register_tool(ToolMetadata(name="failing", description="", input_schema=_INPUT_SCHEMA), failing)
    service.register_tool(ToolMetadata(name="delayed", description="", input_schema=_INPUT_SCHEMA), delayed)
    service.register_tool(ToolMetadata(name="plain", description="", input_schema=_INPUT_SCHEMA), no_context)
    client, service_task = await _ready_client(service)

    try:
        with pytest.raises(ProtocolError, match="expected failure"):
            await client.call_tool("failing", context=_context())

        pending = asyncio.create_task(
            client.call_tool("delayed", context=_context(invocation_id="cancelled"))
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release.set()
        await asyncio.sleep(0)

        assert await client.call_tool("plain") == {"ok": True}
        assert seen_without_context == [None]
        assert service.context_while_sending and all(
            context is None for context in service.context_while_sending
        )
    finally:
        release.set()
        await _close_client(client, service_task)


@pytest.mark.asyncio
async def test_background_task_loses_execution_context_after_rpc_returns():
    """A handler-created task must not retain stale invocation metadata."""

    service = IsolatedFeatureService(name="fake", version="1.0.0")
    inspect_context = asyncio.Event()
    inspected = asyncio.Event()
    observed: list[ToolExecutionContext | None] = []
    children: list[asyncio.Task[None]] = []

    async def child() -> None:
        await inspect_context.wait()
        observed.append(get_tool_execution_context())
        inspected.set()

    async def handler(arguments):
        children.append(asyncio.create_task(child()))
        return {"ok": True}

    service.register_tool(ToolMetadata(name="run", description="", input_schema=_INPUT_SCHEMA), handler)
    client, service_task = await _ready_client(service)

    try:
        assert await client.call_tool("run", context=_context()) == {"ok": True}
        inspect_context.set()
        await asyncio.wait_for(inspected.wait(), timeout=1)
        assert observed == [None]
    finally:
        inspect_context.set()
        if children:
            await asyncio.gather(*children, return_exceptions=True)
        await _close_client(client, service_task)


@pytest.mark.asyncio
async def test_cancelled_sync_worker_loses_execution_context_after_rpc_cancellation():
    """A running ``to_thread`` worker must be revoked when its RPC is cancelled."""

    service = IsolatedFeatureService(name="fake", version="1.0.0")
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    observed: list[ToolExecutionContext | None] = []

    def handler(arguments):
        assert get_tool_execution_context() == _context()
        started.set()
        release.wait(timeout=1)
        observed.append(get_tool_execution_context())
        finished.set()

    service.register_tool(ToolMetadata(name="run", description="", input_schema=_INPUT_SCHEMA), handler)
    request = {
        "name": "run",
        "arguments": {},
        TOOL_EXECUTION_CONTEXT: _context().to_dict(),
    }
    dispatch = asyncio.create_task(
        service._dispatch(JsonRpcRequest(id=1, method=TOOLS_CALL, params=request))
    )

    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1)
        dispatch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatch

        release.set()
        await asyncio.wait_for(asyncio.to_thread(finished.wait), timeout=1)
        assert observed == [None]
    finally:
        release.set()
        if not dispatch.done():
            dispatch.cancel()
        await asyncio.gather(dispatch, return_exceptions=True)


@pytest.mark.asyncio
async def test_legacy_calls_work_in_both_directions_and_context_fails_closed():
    service = IsolatedFeatureService(name="new-service", version="1.0.0")

    async def handler(arguments):
        return {"context": get_tool_execution_context() is not None}

    service.register_tool(ToolMetadata(name="run", description="", input_schema=_INPUT_SCHEMA), handler)
    client, service_task = await _ready_client(service)

    try:
        # An older host does not send an envelope, even when the child is new.
        assert await client.call_tool("run", {"legacy": True}) == {"context": False}
    finally:
        await _close_client(client, service_task)

    class LegacyService(IsolatedFeatureService):
        def __init__(self):
            super().__init__(name="legacy-service", version="1.0.0")
            # Model an older SDK service, which has no execution-context parser
            # and therefore cannot advertise acceptance of the envelope.
            self._tool_execution_context_capabilities = None

    legacy = LegacyService()
    legacy.register_tool(ToolMetadata(name="run", description="", input_schema=_INPUT_SCHEMA), handler)
    legacy_client, legacy_task = await _ready_client(legacy)

    try:
        assert not legacy_client.supports_tool_execution_context
        assert await legacy_client.call_tool("run", {"legacy": True}) == {"context": False}
        with pytest.raises(ToolExecutionContextUnsupportedError, match="does not advertise"):
            await legacy_client.call_tool("run", context=_context())
    finally:
        await _close_client(legacy_client, legacy_task)


@pytest.mark.asyncio
async def test_service_rejects_invalid_oversized_and_reserved_execution_context():
    service = IsolatedFeatureService(name="fake", version="1.0.0")
    service.register_tool(
        ToolMetadata(name="run", description="", input_schema=_INPUT_SCHEMA),
        lambda arguments: {"ok": True},
    )
    client, service_task = await _ready_client(service)
    valid = _context().to_dict()

    try:
        reserved = dict(valid)
        reserved["user_metadata"] = {"secret": "must-not-be-ambient"}
        with pytest.raises(ProtocolError, match="reserved or unknown"):
            await client.request(
                TOOLS_CALL,
                {"name": "run", "arguments": {}, TOOL_EXECUTION_CONTEXT: reserved},
            )

        invalid_type = dict(valid)
        invalid_type["attempt"] = True
        with pytest.raises(ProtocolError, match="integer attempt"):
            await client.request(
                TOOLS_CALL,
                {"name": "run", "arguments": {}, TOOL_EXECUTION_CONTEXT: invalid_type},
            )

        oversized = dict(valid)
        oversized["idempotency_key"] = "x" * 5000
        with pytest.raises(ProtocolError, match="size limit"):
            await client.request(
                TOOLS_CALL,
                {"name": "run", "arguments": {}, TOOL_EXECUTION_CONTEXT: oversized},
            )
    finally:
        await _close_client(client, service_task)
