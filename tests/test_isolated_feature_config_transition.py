"""Negotiated host config-transition lifecycle coverage."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from kestrel_sdk.isolated_feature import (
    CONFIG_TRANSITION,
    HEALTH,
    TOOLS_CALL,
    TOOLS_LIST,
    ConfigTransitionError,
    ConfigTransitionResult,
    ConfigTransitionUnsupportedError,
    IsolatedFeatureClient,
    IsolatedFeatureService,
    ProtocolError,
)
from kestrel_sdk.isolated_feature.client import SubprocessIsolatedFeatureClient

from .test_isolated_feature import memory_stdio_pair


async def _wait_for_state_records(path, count: int) -> list[dict[str, object]]:
    """Read append-only subprocess observations without a timing-only sleep."""

    deadline = asyncio.get_running_loop().time() + 2
    while True:
        if path.exists():
            records = [json.loads(line) for line in path.read_text().splitlines()]
            if len(records) >= count:
                return records
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"expected {count} state records")
        await asyncio.sleep(0.01)


_TRANSITION_SUBPROCESS = r"""
import asyncio
import json
import os

from kestrel_sdk.isolated_feature import ConfigTransitionResult, IsolatedFeatureService


def record(event, **values):
    with open(os.environ["ISOLATED_FEATURE_STATE_FILE"], "a", encoding="utf-8") as file:
        file.write(json.dumps({"event": event, **values}) + "\n")
        file.flush()


class TransitionService(IsolatedFeatureService):
    def __init__(self):
        super().__init__(name="transition-subprocess", version="1.0.0")
        self.advertise_config_transition()

    async def configure(self, config):
        record("initialize", config=config)

    async def on_config_transition(self, next_config):
        record("transition-started", old_config=self.host_config, next_config=next_config)
        await asyncio.sleep(0.2)
        record("transition-finished")
        return ConfigTransitionResult.restart_required()


asyncio.run(TransitionService().run_stdio())
"""


_HANGING_HEALTH_SUBPROCESS = r"""
import asyncio
import json
import os

from kestrel_sdk.isolated_feature import IsolatedFeatureService


def record(event, **values):
    with open(os.environ["ISOLATED_FEATURE_STATE_FILE"], "a", encoding="utf-8") as file:
        file.write(json.dumps({"event": event, **values}) + "\n")
        file.flush()


class HangingHealthService(IsolatedFeatureService):
    def __init__(self):
        super().__init__(name="hanging-health", version="1.0.0")
        self.release_health = asyncio.Event()

    async def configure(self, config):
        record("initialize", config=config)

    async def health(self):
        record("health-started")
        await self.release_health.wait()
        return {"status": "ready", "ready": True}

    async def on_shutdown(self):
        self.release_health.set()
        return await super().on_shutdown()


asyncio.run(HangingHealthService().run_stdio())
"""


_WINDOWS_STDIO_SUBPROCESS = r"""
import asyncio
import json
import os
import sys

from kestrel_sdk.isolated_feature import IsolatedFeatureService


def record(event, **values):
    with open(os.environ["ISOLATED_FEATURE_STATE_FILE"], "a", encoding="utf-8") as file:
        file.write(json.dumps({"event": event, **values}) + "\n")
        file.flush()


class WindowsStdioService(IsolatedFeatureService):
    def __init__(self):
        super().__init__(name="windows-stdio", version="1.0.0")

    async def configure(self, config):
        record("initialize", config=config)

    async def health(self):
        record("health")
        return {"status": "ready", "ready": True}


async def main():
    # Exercise the Windows inherited-stdio adapter on every CI host. Replacing
    # the base methods makes this an assertion about the selected
    # implementation rather than a platform skip: the legacy pipe setup fails
    # before initialize. Do this after asyncio.run() has made its native loop;
    # forcing sys.platform earlier would make a non-Windows interpreter import
    # Windows-only event-loop modules.
    sys.platform = "win32"

    original_fdopen = os.fdopen

    def checked_fdopen(fd, *args, **kwargs):
        record("wire", inheritable=os.get_inheritable(fd))
        return original_fdopen(fd, *args, **kwargs)

    os.fdopen = checked_fdopen

    def unexpected_proactor_pipe_setup(*args, **kwargs):
        raise AssertionError(
            "Windows inherited stdio must not use asyncio pipe transports"
        )

    asyncio.BaseEventLoop.connect_read_pipe = unexpected_proactor_pipe_setup
    asyncio.BaseEventLoop.connect_write_pipe = unexpected_proactor_pipe_setup
    await WindowsStdioService().run_stdio()


asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_legacy_service_does_not_advertise_or_receive_transition_requests():
    """New clients use a conservative replacement fallback for legacy services."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    service = IsolatedFeatureService(name="legacy", version="1.0.0")
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        initialized = await client.initialize(config={"token": "old-token"})
        assert "config_transition" not in initialized["capabilities"]
        assert client.supports_config_transition is False

        with pytest.raises(ConfigTransitionUnsupportedError):
            await client.prepare_config_transition({"token": "next-token"})

        # The transition is lifecycle-only, not smuggled into tools/list.
        await client.health()
        assert await client.list_tools() == []
        await client.shutdown()
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_transition_round_trip_receives_next_config_with_old_config_intact():
    """A prepare hook can use old credentials before requesting replacement."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    observed: list[tuple[dict[str, object], dict[str, object]]] = []

    class TransitionService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="transition", version="1.0.0")
            self.advertise_config_transition()

        async def on_config_transition(self, next_config):
            observed.append((self.host_config, next_config))
            return ConfigTransitionResult.restart_required()

    service = TransitionService()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)
    old_config = {"token": "old-secret", "webhook_url": "https://old.example"}
    next_config = {"token": "next-secret", "webhook_url": "https://next.example"}

    try:
        initialized = await client.initialize(config=old_config)
        assert initialized["capabilities"]["config_transition"] == {
            "prepare": True,
            "supports_live_apply": False,
        }
        assert client.supports_config_transition is True

        result = await client.prepare_config_transition(next_config)
        assert result == ConfigTransitionResult.restart_required()
        assert observed == [(old_config, next_config)]
        # Prepare-only never mutates the running service's old effective config.
        assert service.host_config == old_config
        assert await client.health() == {"status": "restart-required", "ready": False}

        await client.shutdown()
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_live_apply_requires_capability_and_commits_config_after_hook():
    """An explicitly live-apply-capable service may remain running."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    old_config = {"transport": "polling"}
    next_config = {"transport": "webhook"}

    class LiveApplyService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="live", version="1.0.0")
            self.advertise_config_transition(supports_live_apply=True)
            self.old_config_seen: dict[str, object] | None = None

        async def on_config_transition(self, next_config):
            self.old_config_seen = self.host_config
            return ConfigTransitionResult.applied()

    service = LiveApplyService()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        await client.initialize(config=old_config)
        assert client.config_transition_capabilities is not None
        assert client.config_transition_capabilities.supports_live_apply is True

        assert await client.prepare_config_transition(next_config) == ConfigTransitionResult.applied()
        assert service.old_config_seen == old_config
        assert service.host_config == next_config
        assert await client.health() == {"status": "ready", "ready": True}

        await client.shutdown()
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_transition_failure_is_returned_without_reflecting_secrets(caplog):
    """Hook exceptions fail the host request but never echo config payloads."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    secret = "telegram-token-12345"

    class FailingService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="failing", version="1.0.0")
            self.advertise_config_transition()

        async def on_config_transition(self, next_config):
            raise RuntimeError(f"unable to retire webhook for {next_config['token']}")

    service = FailingService()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)
    old_config = {"token": "old-token"}

    try:
        await client.initialize(config=old_config)
        with pytest.raises(ConfigTransitionError) as error:
            await client.prepare_config_transition({"token": secret})

        assert str(error.value) == "config transition failed"
        assert error.value.__cause__ is not None
        assert secret not in str(error.value.__cause__)
        assert secret not in caplog.text
        assert service.host_config == old_config
        assert service._restart_required is False

        await client.shutdown()
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_transition_shutdown_and_health_are_deterministically_serialized():
    """A host transition completes before its concurrent shutdown may begin."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    transition_started = asyncio.Event()
    release_transition = asyncio.Event()
    lifecycle_order: list[str] = []

    class BlockingService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="blocking", version="1.0.0")
            self.advertise_config_transition()

        async def on_config_transition(self, next_config):
            lifecycle_order.append("transition")
            transition_started.set()
            await release_transition.wait()
            return ConfigTransitionResult.restart_required()

        async def on_shutdown(self):
            lifecycle_order.append("shutdown")
            return await super().on_shutdown()

    service = BlockingService()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        await client.initialize(config={"enabled": True})
        transition_task = asyncio.create_task(
            client.prepare_config_transition({"enabled": False})
        )
        await asyncio.wait_for(transition_started.wait(), timeout=1)

        # HEALTH may be sent while a hook is running, but the inline lifecycle
        # request keeps it queued until the transition reaches an outcome.
        health_task = asyncio.create_task(client.health())
        shutdown_task = asyncio.create_task(client.shutdown())
        await asyncio.sleep(0)
        assert health_task.done() is False
        assert shutdown_task.done() is False
        assert lifecycle_order == ["transition"]

        release_transition.set()
        assert await transition_task == ConfigTransitionResult.restart_required()
        assert await health_task == {"status": "restart-required", "ready": False}
        assert await shutdown_task == {"ok": True}
        assert lifecycle_order == ["transition", "shutdown"]
        await service_task
    finally:
        release_transition.set()
        if not service_task.done():
            service_reader.close()
            await asyncio.gather(service_task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_timed_out_transition_discards_late_response_and_requires_replacement():
    """A timed-out request cannot poison later shutdown/read-loop traffic."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    transition_started = asyncio.Event()
    release_transition = asyncio.Event()

    class BlockingService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="blocking", version="1.0.0")
            self.advertise_config_transition()

        async def on_config_transition(self, next_config):
            transition_started.set()
            await release_transition.wait()
            return ConfigTransitionResult.restart_required()

    service = BlockingService()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        await client.initialize(config={"token": "old"})
        transition_task = asyncio.create_task(
            client.prepare_config_transition({"token": "next"})
        )
        await asyncio.wait_for(transition_started.wait(), timeout=1)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(transition_task, timeout=0.01)

        # The cancelled waiter has been removed; its late reply is discarded.
        assert client._pending == {}
        assert await client.health() == {"status": "restart-required", "ready": False}
        with pytest.raises(ConfigTransitionError, match="replacement is already required"):
            await client.prepare_config_transition({"token": "another"})

        # The child still completes the request that was already on the wire.
        # A later shutdown proves the read loop survived its late response.
        release_transition.set()
        assert await client.shutdown() == {"ok": True}
        await service_task
    finally:
        release_transition.set()
        if not service_task.done():
            service_reader.close()
            await asyncio.gather(service_task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_restart_required_rejects_repeated_transitions_client_and_service_side():
    """A prepare-only result retires this instance exactly once."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    calls: list[dict[str, object]] = []

    class RestartService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="restart", version="1.0.0")
            self.advertise_config_transition()

        async def on_config_transition(self, next_config):
            calls.append(next_config)
            return ConfigTransitionResult.restart_required()

    service = RestartService()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        await client.initialize(config={"version": 1})
        assert await client.prepare_config_transition({"version": 2}) == (
            ConfigTransitionResult.restart_required()
        )

        # The typed client fails before sending another lifecycle request.
        with pytest.raises(ConfigTransitionError, match="replacement is already required"):
            await client.prepare_config_transition({"version": 3})

        # A raw request from a buggy/older caller is fenced by the service too.
        with pytest.raises(ProtocolError, match="config transition failed"):
            await client.request(CONFIG_TRANSITION, {"config": {"version": 3}})
        assert calls == [{"version": 2}]

        await client.shutdown()
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wrapper_retains_first_replacement_config_after_later_rejection():
    """A local rejection cannot steal config ownership from the fenced child."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    calls: list[dict[str, object]] = []

    class RestartService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="restart", version="1.0.0")
            self.advertise_config_transition()

        async def on_config_transition(self, next_config):
            calls.append(next_config)
            return ConfigTransitionResult.restart_required()

    service = RestartService()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)
    first_config = {"version": 1}
    replacement_config = {"version": 2}
    rejected_config = {"version": 3}
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], config=first_config, client=client
    )

    try:
        await client.initialize(config=first_config)
        assert await wrapper.prepare_config_transition(replacement_config) == (
            ConfigTransitionResult.restart_required()
        )
        with pytest.raises(ConfigTransitionError, match="replacement is already required"):
            await wrapper.prepare_config_transition(rejected_config)

        assert calls == [replacement_config]
        assert wrapper.config is replacement_config
        assert wrapper.replacement_required is True

        await client.shutdown()
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_restart_fencing_cannot_be_bypassed_by_overridden_health_or_tools():
    """Dispatch owns the retired-service boundary, not feature overrides."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()

    class OverrideService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="override", version="1.0.0")
            self.advertise_config_transition()
            self.health_called = False
            self.tools_called = False

        async def on_config_transition(self, next_config):
            return ConfigTransitionResult.restart_required()

        async def health(self):
            self.health_called = True
            return {"status": "ready", "ready": True}

        async def get_tools(self):
            self.tools_called = True
            return []

        async def call_tool(self, name, arguments):
            self.tools_called = True
            return {"unexpected": True}

    service = OverrideService()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        await client.initialize(config={"enabled": True})
        await client.prepare_config_transition({"enabled": False})

        # Use raw requests to exercise service dispatch rather than the
        # client's own replacement fence.
        assert await client.request(HEALTH) == {"status": "restart-required", "ready": False}
        with pytest.raises(ProtocolError, match="service is awaiting restart"):
            await client.request(TOOLS_LIST)
        with pytest.raises(ProtocolError, match="service is awaiting restart"):
            await client.request(TOOLS_CALL, {"name": "ignored", "arguments": {}})
        assert service.health_called is False
        assert service.tools_called is False

        await client.shutdown()
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_transition_stream_failure_uses_typed_error():
    """A child-side EOF is never exposed as an untyped lifecycle failure."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    transition_started = asyncio.Event()
    release_transition = asyncio.Event()

    class BlockingService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="blocking", version="1.0.0")
            self.advertise_config_transition()

        async def on_config_transition(self, next_config):
            transition_started.set()
            await release_transition.wait()
            return ConfigTransitionResult.restart_required()

    service = BlockingService()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        await client.initialize(config={"token": "old"})
        transition_task = asyncio.create_task(
            client.prepare_config_transition({"token": "next"})
        )
        await asyncio.wait_for(transition_started.wait(), timeout=1)

        # Simulate the feature process exiting before it answers the lifecycle
        # request. This closes the actual client reader, not a mocked request.
        host_reader.close()
        with pytest.raises(ConfigTransitionError, match="config transition failed") as error:
            await transition_task
        assert isinstance(error.value.__cause__, EOFError)
        assert client._restart_required is True
    finally:
        release_transition.set()
        service_reader.close()
        await asyncio.gather(service_task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_terminal_protocol_error_fences_client_and_retains_next_wrapper_config():
    """Malformed child output is transport failure, not a recoverable hook error."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    transition_started = asyncio.Event()
    release_transition = asyncio.Event()

    class BlockingService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="blocking", version="1.0.0")
            self.advertise_config_transition()

        async def on_config_transition(self, next_config):
            transition_started.set()
            await release_transition.wait()
            return ConfigTransitionResult.restart_required()

    service = BlockingService()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)
    old_config = {"token": "old"}
    next_config = {"token": "next"}
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], config=old_config, client=client
    )

    try:
        await client.initialize(config=old_config)
        transition_task = asyncio.create_task(wrapper.prepare_config_transition(next_config))
        await asyncio.wait_for(transition_started.wait(), timeout=1)

        # This goes directly to the client's real reader loop. It cannot be a
        # JSON-RPC hook rejection because it has no valid response envelope.
        service_writer.write(b"not-json\n")
        with pytest.raises(ConfigTransitionError, match="config transition failed"):
            await transition_task

        assert isinstance(client._closed_exc, ProtocolError)
        assert client.replacement_required is True
        assert wrapper.replacement_required is True
        assert wrapper.config is next_config
    finally:
        release_transition.set()
        service_reader.close()
        await asyncio.gather(service_task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_wrapper_rolls_back_config_after_rejected_transition():
    """A hook error leaves the old child and its next-start config unchanged."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()

    class FailingService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="failing", version="1.0.0")
            self.advertise_config_transition()

        async def on_config_transition(self, next_config):
            raise RuntimeError("cleanup failed")

    service = FailingService()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)
    old_config = {"token": "old"}
    next_config = {"token": "next"}
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], config=old_config, client=client
    )

    try:
        await client.initialize(config=old_config)
        with pytest.raises(ConfigTransitionError, match="config transition failed"):
            await wrapper.prepare_config_transition(next_config)

        assert wrapper.config is old_config
        assert wrapper.replacement_required is False
        await client.shutdown()
        await service_task
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wrapper_serializes_inflight_health_before_transition_state_change():
    """A probe that started first cannot span a wrapper config replacement."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    health_started = asyncio.Event()
    release_health = asyncio.Event()
    transition_started = asyncio.Event()

    class BlockingHealthService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="blocking-health", version="1.0.0")
            self.advertise_config_transition()

        async def health(self):
            health_started.set()
            await release_health.wait()
            return {"status": "ready", "ready": True}

        async def on_config_transition(self, next_config):
            transition_started.set()
            return ConfigTransitionResult.restart_required()

    service = BlockingHealthService()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)
    old_config = {"enabled": True}
    next_config = {"enabled": False}
    wrapper = SubprocessIsolatedFeatureClient(
        command=["unused"], config=old_config, client=client
    )

    try:
        await client.initialize(config=old_config)
        health_task = asyncio.create_task(wrapper.health())
        await asyncio.wait_for(health_started.wait(), timeout=1)
        transition_task = asyncio.create_task(wrapper.prepare_config_transition(next_config))
        await asyncio.sleep(0)
        assert transition_started.is_set() is False

        release_health.set()
        assert await health_task == {"status": "ready", "ready": True}
        assert await transition_task == ConfigTransitionResult.restart_required()
        assert wrapper.config is next_config
        assert wrapper.replacement_required is True
        assert await wrapper.health() == {"status": "restart-required", "ready": False}

        await client.shutdown()
        await service_task
    finally:
        release_health.set()
        if not service_task.done():
            service_reader.close()
            await asyncio.gather(service_task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_wrapper_stop_interrupts_real_subprocess_with_wedged_startup_health(tmp_path):
    """Stop reaches the child even while start is awaiting an unbounded health RPC."""
    state_file = tmp_path / "hanging-health-state.jsonl"
    wrapper = SubprocessIsolatedFeatureClient(
        command=[sys.executable, "-u", "-c", _HANGING_HEALTH_SUBPROCESS],
        env={**os.environ, "ISOLATED_FEATURE_STATE_FILE": str(state_file)},
        config={"enabled": True},
    )
    start_task = asyncio.create_task(wrapper.start())

    try:
        records = await _wait_for_state_records(state_file, 2)
        assert records == [
            {"event": "initialize", "config": {"enabled": True}},
            {"event": "health-started"},
        ]

        # Under the old single wrapper lock, this waited forever behind
        # start()'s health request and never reached the shutdown/terminate path.
        await asyncio.wait_for(wrapper.stop(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await start_task
        assert wrapper.client is None
        assert wrapper.process is None
    finally:
        if not start_task.done():
            start_task.cancel()
            await asyncio.gather(start_task, return_exceptions=True)
        await wrapper.stop()


@pytest.mark.asyncio
async def test_wrapper_uses_windows_inherited_stdio_adapter_without_proactor_pipes(
    tmp_path,
):
    """Windows child stdio stays usable without Proactor pipe ownership."""
    state_file = tmp_path / "windows-stdio-state.jsonl"
    config = {"enabled": True}
    wrapper = SubprocessIsolatedFeatureClient(
        command=[sys.executable, "-u", "-c", _WINDOWS_STDIO_SUBPROCESS],
        env={**os.environ, "ISOLATED_FEATURE_STATE_FILE": str(state_file)},
        config=config,
    )

    try:
        await wrapper.start()
        records = await _wait_for_state_records(state_file, 3)
        assert records == [
            {"event": "wire", "inheritable": False},
            {"event": "initialize", "config": config},
            {"event": "health"},
        ]
        assert await wrapper.health() == {"status": "ready", "ready": True}
        await asyncio.wait_for(wrapper.stop(), timeout=1)
        assert wrapper.client is None
        assert wrapper.process is None
    finally:
        await wrapper.stop()


@pytest.mark.asyncio
async def test_late_health_reply_cannot_restore_ready_after_restart_transition():
    """A probe started before cleanup cannot revive a fenced inner client."""
    host_reader, host_writer, service_reader, service_writer = memory_stdio_pair()
    health_started = asyncio.Event()
    release_health = asyncio.Event()
    transition_started = asyncio.Event()

    class InterleavedService(IsolatedFeatureService):
        def __init__(self) -> None:
            super().__init__(name="interleaved", version="1.0.0")
            self.advertise_config_transition()

        async def health(self):
            health_started.set()
            await release_health.wait()
            return {"status": "ready", "ready": True}

        async def on_config_transition(self, next_config):
            transition_started.set()
            return ConfigTransitionResult.restart_required()

    service = InterleavedService()
    service_task = asyncio.create_task(service.serve(service_reader, service_writer))
    client = IsolatedFeatureClient(host_reader, host_writer)

    try:
        await client.initialize(config={"enabled": True})
        health_task = asyncio.create_task(client.health())
        await asyncio.wait_for(health_started.wait(), timeout=1)

        transition_task = asyncio.create_task(
            client.prepare_config_transition({"enabled": False})
        )
        await asyncio.wait_for(transition_started.wait(), timeout=1)
        assert await transition_task == ConfigTransitionResult.restart_required()

        release_health.set()
        assert await health_task == {"status": "restart-required", "ready": False}
        assert client.ready is False
        assert client.replacement_required is True

        await client.shutdown()
        await service_task
    finally:
        release_health.set()
        if not service_task.done():
            service_reader.close()
            await asyncio.gather(service_task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_wrapper_timeout_restarts_subprocess_with_requested_next_config(tmp_path):
    """A late response after timeout cannot make a replacement use old config."""
    state_file = tmp_path / "transition-state.jsonl"
    old_config = {"token": "old-token", "enabled": True}
    next_config = {"token": "next-token", "enabled": False}
    wrapper = SubprocessIsolatedFeatureClient(
        command=[sys.executable, "-u", "-c", _TRANSITION_SUBPROCESS],
        env={**os.environ, "ISOLATED_FEATURE_STATE_FILE": str(state_file)},
        config=old_config,
    )

    try:
        await wrapper.start()
        records = await _wait_for_state_records(state_file, 1)
        assert records == [{"event": "initialize", "config": old_config}]

        transition_task = asyncio.create_task(wrapper.prepare_config_transition(next_config))
        records = await _wait_for_state_records(state_file, 2)
        assert records[1] == {
            "event": "transition-started",
            "old_config": old_config,
            "next_config": next_config,
        }

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(transition_task, timeout=0.01)

        assert wrapper.config is next_config
        assert wrapper.replacement_required is True

        # The child hook is deliberately not cancelled. Wait for its late
        # response to traverse the actual JSON-RPC reader before replacing it.
        records = await _wait_for_state_records(state_file, 3)
        assert records[2] == {"event": "transition-finished"}

        await wrapper.stop()
        await wrapper.start()
        records = await _wait_for_state_records(state_file, 4)
        assert records[3] == {"event": "initialize", "config": next_config}
    finally:
        await wrapper.stop()
