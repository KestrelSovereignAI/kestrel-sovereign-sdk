"""Tests for the host-scoped feature contract (issue #46).

Exercises a fixture ``HostFeature`` subclass across router + lifecycle +
store handle + UI contributions, and asserts the contract stays free of any
subagent/agent binding and importable from the SDK top level.
"""

from __future__ import annotations

import pytest

from kestrel_sdk import HostContext, HostFeature, UIContributions
from kestrel_sdk.features.base import Feature
from kestrel_sdk.features import validate_feature_contributions
from kestrel_sdk.storage.database import (
    DatabaseBackend,
    EngineTarget,
    PrivacyMode,
)


# ---------------------------------------------------------------------------
# Fixtures: a minimal host runtime context + host feature.
# ---------------------------------------------------------------------------


class _FakeBackplane:
    def __init__(self):
        self.published: list = []

    async def publish(self, topic, payload):
        self.published.append((topic, payload))


class _FakeBackend(DatabaseBackend):
    """Smoke host backend implementing the SDK's DatabaseBackend ABC."""

    @property
    def backend_type(self):
        return "sqlite"

    @property
    def is_connected(self):
        return True

    async def connect(self):
        pass

    async def close(self):
        pass

    async def execute(self, query, params=()):
        return 0

    async def execute_many(self, query, params_list):
        return 0

    async def fetch_one(self, query, params=()):
        return None

    async def fetch_all(self, query, params=()):
        return []

    async def fetch_val(self, query, params=()):
        return None

    async def execute_script(self, script):
        pass

    async def transaction(self):  # type: ignore[override]
        yield


class _FakeHostContext:
    """Concrete host context conforming to the HostContext Protocol."""

    def __init__(self):
        self._db = _FakeBackend()
        self._backplane = _FakeBackplane()
        self._config = {"host_db_url": "postgresql+asyncpg://u:p@h/fleet"}

    @property
    def db(self):
        return self._db

    @property
    def backplane(self):
        return self._backplane

    @property
    def config(self):
        return self._config


class _StubRouter:
    """Duck-typed stand-in for a FastAPI APIRouter (SDK ships no fastapi dep)."""

    def __init__(self):
        self.routes: list = []

    def add(self, path):
        self.routes.append(path)


class ExampleHostFeature(HostFeature):
    """Minimal example host feature: router + lifecycle + store handle + UI."""

    name = "fleet-observability"
    capability = "fleet.observe"

    def __init__(self):
        self.started = False
        self.stopped = False
        self.engine_target: EngineTarget | None = None
        self.backend: DatabaseBackend | None = None

    def get_router(self):
        router = _StubRouter()
        router.add("/fleet/health")
        return router

    async def on_host_start(self, ctx: HostContext) -> None:
        # Bind a host-scoped store from the SDK's own storage primitives.
        self.engine_target = self.resolve_host_engine_target(
            ctx.config["host_db_url"]
        )
        self.backend = ctx.db
        await ctx.backplane.publish("fleet.lifecycle", "started")
        self.started = True

    async def on_host_stop(self, ctx: HostContext) -> None:
        await ctx.backplane.publish("fleet.lifecycle", "stopped")
        self.stopped = True

    def get_ui_contributions(self):
        return UIContributions(
            static_dir="/pkg/fleet_observability/static",
            modules=["fleet-panel.js"],
            capability=self.capability,
        )


# ---------------------------------------------------------------------------
# Import surface / acceptance criteria.
# ---------------------------------------------------------------------------


def test_hostfeature_importable_from_sdk_top_level():
    from kestrel_sdk import HostFeature as TopLevel

    assert TopLevel is HostFeature
    assert "HostFeature" in __import__("kestrel_sdk").__all__


def test_hostfeature_is_abc_and_distinct_from_feature():
    assert issubclass(HostFeature, object)
    assert not issubclass(HostFeature, Feature)
    assert not issubclass(Feature, HostFeature)


def test_hostfeature_has_no_agent_binding():
    """The contract must not reference any subagent/agent surface."""
    # No agent-scoped attributes/methods on the class.
    for banned in ("agent", "get_agent", "tool_description", "to_orchestrator_tool"):
        assert not hasattr(HostFeature, banned), f"leaked agent surface: {banned}"

    # __init__ takes no agent (unlike Feature.__init__(self, agent)).
    feature = ExampleHostFeature()
    assert not hasattr(feature, "agent")


def test_declares_required_contract_methods():
    for name in (
        "get_router",
        "on_host_start",
        "on_host_stop",
        "resolve_host_engine_target",
        "get_ui_contributions",
        "get_service_registrations",
        "get_wait_provider_registrations",
        "get_workflow_registrations",
        "get_feature_permission_defaults",
        "get_setup_step_registrations",
    ):
        assert hasattr(HostFeature, name), f"missing contract method: {name}"


# ---------------------------------------------------------------------------
# Behaviour: router + lifecycle + store handle + UI.
# ---------------------------------------------------------------------------


def test_name_and_capability_slugs():
    feature = ExampleHostFeature()
    assert feature.name == "fleet-observability"
    assert feature.contribution_owner == (
        f"{ExampleHostFeature.__module__}:{ExampleHostFeature.__qualname__}"
    )
    assert feature.capability == "fleet.observe"
    # Base defaults are sane / ungated.
    assert HostFeature.name == "host-feature"
    assert HostFeature.capability is None


def test_base_defaults_are_thin():
    """Bare subclass returns no router / no UI and no-op lifecycle."""

    class Bare(HostFeature):
        pass

    bare = Bare()
    assert bare.get_router() is None
    assert bare.get_ui_contributions() is None
    assert bare.get_service_registrations() == ()
    assert bare.get_wait_provider_registrations() == ()
    assert bare.get_workflow_registrations() == ()
    assert bare.get_feature_permission_defaults() is None
    assert bare.get_setup_step_registrations() == ()
    assert "Bare" in bare.contribution_owner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_owner",
    ["Legacy Host Display Name", None, pytest.param(object(), id="object")],
)
async def test_host_contribution_owner_does_not_claim_legacy_owner(
    legacy_owner: object,
):
    class OtherBare(HostFeature):
        pass

    class LegacyOwnerHost(HostFeature):
        name = "Fleet Operations Display"

        def __init__(self):
            self.owner = legacy_owner

        async def on_host_start(self, ctx: HostContext) -> None:
            self.name = "later-valid-name"

    bare = type("BareHost", (HostFeature,), {})()
    other = OtherBare()
    before_read = LegacyOwnerHost()
    after_read = LegacyOwnerHost()
    canonical = before_read.contribution_owner
    await before_read.on_host_start(object())  # type: ignore[arg-type]
    await after_read.on_host_start(object())  # type: ignore[arg-type]

    assert "owner" not in HostFeature.__dict__
    assert "BareHost" in bare.contribution_owner
    assert "OtherBare" in other.contribution_owner
    assert bare.contribution_owner != other.contribution_owner
    assert before_read.owner is legacy_owner
    assert after_read.owner is legacy_owner
    assert "LegacyOwnerHost" in canonical
    assert before_read.contribution_owner == canonical
    assert after_read.contribution_owner == canonical
    validate_feature_contributions(
        canonical,
        tool_names=(),
        services=(),
        wait_providers=(),
        workflows=(),
        permission_defaults=None,
        setup_steps=(),
    )


def test_get_router_mounts_at_host_root():
    router = ExampleHostFeature().get_router()
    assert router is not None
    assert "/fleet/health" in router.routes


def test_ui_contributions_shape():
    ui = ExampleHostFeature().get_ui_contributions()
    assert isinstance(ui, UIContributions)
    assert ui.static_dir == "/pkg/fleet_observability/static"
    assert ui.modules == ["fleet-panel.js"]
    assert ui.capability == "fleet.observe"


def test_resolve_host_engine_target_uses_sdk_storage():
    feature = ExampleHostFeature()
    target = feature.resolve_host_engine_target("postgresql+asyncpg://u:p@h/fleet")
    assert isinstance(target, EngineTarget)
    assert target.url == "postgresql+asyncpg://u:p@h/fleet"
    assert target.persistent is True


def test_resolve_host_engine_target_volatile_mode():
    feature = ExampleHostFeature()
    target = feature.resolve_host_engine_target(None, mode=PrivacyMode.EPHEMERAL)
    assert target.url == "sqlite+aiosqlite:///:memory:"
    assert target.persistent is False


@pytest.mark.asyncio
async def test_host_lifecycle_start_stop():
    feature = ExampleHostFeature()
    ctx = _FakeHostContext()

    assert isinstance(ctx, HostContext)  # runtime_checkable Protocol

    await feature.on_host_start(ctx)
    assert feature.started is True
    assert feature.backend is ctx.db
    assert isinstance(feature.engine_target, EngineTarget)
    assert feature.engine_target.persistent is True
    assert ("fleet.lifecycle", "started") in ctx.backplane.published

    await feature.on_host_stop(ctx)
    assert feature.stopped is True
    assert ("fleet.lifecycle", "stopped") in ctx.backplane.published


def test_hostcontext_protocol_rejects_incomplete_object():
    class Missing:
        @property
        def db(self):
            return None

    # Lacks backplane + config → not a HostContext.
    assert not isinstance(Missing(), HostContext)
