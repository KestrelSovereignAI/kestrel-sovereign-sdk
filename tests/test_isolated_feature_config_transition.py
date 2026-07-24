"""Negotiated host config-transition lifecycle coverage."""

from __future__ import annotations

import asyncio

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

from .test_isolated_feature import memory_stdio_pair


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
