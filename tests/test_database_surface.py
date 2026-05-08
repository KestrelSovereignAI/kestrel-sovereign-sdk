"""Tests for kestrel_sdk.storage.database surface (issue #1094)."""

from __future__ import annotations

import os

import pytest

from kestrel_sdk.storage.database import (
    ConnectionError,
    DatabaseBackend,
    DatabaseError,
    EngineTarget,
    PrivacyMode,
    QueryError,
    TransactionError,
    resolve_engine_target,
)


def test_module_exports_match_acceptance_criteria():
    import kestrel_sdk.storage.database as mod

    expected = {
        "DatabaseBackend",
        "DatabaseError",
        "ConnectionError",
        "QueryError",
        "TransactionError",
        "Params",
        "Row",
        "PrivacyMode",
        "EngineTarget",
        "resolve_engine_target",
    }
    assert expected.issubset(set(mod.__all__))
    for name in expected:
        assert hasattr(mod, name), f"missing export: {name}"


def test_database_backend_is_abstract():
    assert issubclass(DatabaseBackend, object)
    with pytest.raises(TypeError):
        DatabaseBackend()  # type: ignore[abstract]


def test_error_hierarchy():
    assert issubclass(ConnectionError, DatabaseError)
    assert issubclass(QueryError, DatabaseError)
    assert issubclass(TransactionError, DatabaseError)
    assert issubclass(DatabaseError, Exception)


def test_privacy_mode_string_round_trip():
    assert PrivacyMode("normal") is PrivacyMode.NORMAL
    assert PrivacyMode.NORMAL == "normal"
    assert PrivacyMode.EPHEMERAL.value == "ephemeral"
    assert {m.value for m in PrivacyMode} == {
        "ephemeral",
        "isolated",
        "anonymous",
        "normal",
        "public",
    }


def test_privacy_mode_hashes_identical_to_string():
    """str-Enum mixin: dict/set keyed by enum collide with raw strings."""
    assert hash(PrivacyMode.NORMAL) == hash("normal")
    d = {PrivacyMode.NORMAL: 1}
    assert d["normal"] == 1


def test_resolve_ephemeral_ignores_fallback():
    target = resolve_engine_target(PrivacyMode.EPHEMERAL, "postgresql://prod/db")
    assert target.url == "sqlite+aiosqlite:///:memory:"
    assert target.persistent is False
    assert target.cleanup_path is None
    assert "ephemeral" in target.description


def test_resolve_isolated_creates_tempfile_and_is_volatile():
    target = resolve_engine_target(PrivacyMode.ISOLATED, "postgresql://prod/db")
    assert target.url.startswith("sqlite+aiosqlite:///")
    assert target.persistent is False
    assert target.cleanup_path is not None
    assert os.path.exists(target.cleanup_path)
    target.cleanup()
    assert not os.path.exists(target.cleanup_path)


def test_isolated_cleanup_is_idempotent():
    target = resolve_engine_target(PrivacyMode.ISOLATED, None)
    target.cleanup()
    # Second call must not raise even though the file is already gone.
    target.cleanup()


def test_non_isolated_cleanup_is_noop():
    target = resolve_engine_target(PrivacyMode.EPHEMERAL, None)
    target.cleanup()  # cleanup_path is None — must not raise


@pytest.mark.parametrize(
    "mode", [PrivacyMode.ANONYMOUS, PrivacyMode.NORMAL, PrivacyMode.PUBLIC]
)
def test_resolve_persistent_modes_pass_fallback_through(mode):
    target = resolve_engine_target(mode, "postgresql+asyncpg://u:p@h/db")
    assert target.url == "postgresql+asyncpg://u:p@h/db"
    assert target.persistent is True
    assert target.cleanup_path is None
    assert mode.value in target.description


@pytest.mark.parametrize(
    "mode_value", ["anonymous", "normal", "public"]
)
def test_resolve_accepts_raw_string_for_persistent_modes(mode_value):
    target = resolve_engine_target(mode_value, "postgresql+asyncpg://u:p@h/db")
    assert target.persistent is True
    assert mode_value in target.description


@pytest.mark.parametrize(
    "mode_value", ["ephemeral", "isolated"]
)
def test_resolve_accepts_raw_string_for_volatile_modes(mode_value):
    target = resolve_engine_target(mode_value, None)
    assert target.persistent is False
    if mode_value == "isolated":
        target.cleanup()


def test_resolve_rejects_unknown_string_mode():
    with pytest.raises(ValueError):
        resolve_engine_target("definitely-not-a-mode", "postgresql://x")


def test_resolve_accepts_unrelated_enum_with_matching_value():
    """Sovereign's pre-#1094 `PrivacyMode(Enum)` had `.value == "normal"`.
    `resolve_engine_target` should accept any Enum whose value matches a
    canonical mode name — strip `.value` before coercion."""
    from enum import Enum as _Enum

    class _LegacyMode(_Enum):
        NORMAL = "normal"
        EPHEMERAL = "ephemeral"

    persistent = resolve_engine_target(_LegacyMode.NORMAL, "postgresql://x")
    assert persistent.persistent is True

    volatile = resolve_engine_target(_LegacyMode.EPHEMERAL, None)
    assert volatile.url == "sqlite+aiosqlite:///:memory:"


@pytest.mark.parametrize(
    "mode", [PrivacyMode.ANONYMOUS, PrivacyMode.NORMAL, PrivacyMode.PUBLIC]
)
@pytest.mark.parametrize("bad", [None, "", "   ", "\t\n"])
def test_resolve_persistent_requires_non_blank_fallback(mode, bad):
    with pytest.raises(ValueError, match="fallback_url"):
        resolve_engine_target(mode, bad)


def test_engine_target_is_frozen():
    target = resolve_engine_target(PrivacyMode.EPHEMERAL, None)
    assert isinstance(target, EngineTarget)
    with pytest.raises(Exception):
        target.url = "x"  # type: ignore[misc]


class _MinimalBackend(DatabaseBackend):
    """Smoke-test fixture: any class implementing the abstract surface
    must instantiate cleanly. Catches accidental ABC method drift."""

    @property
    def backend_type(self):
        return "sqlite"

    @property
    def is_connected(self):
        return False

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
        # Real backends use @asynccontextmanager; the smoke test only
        # verifies the abstract method names are stable.
        yield


def test_minimal_backend_instantiates():
    backend = _MinimalBackend()
    assert backend.backend_type == "sqlite"
    assert backend.is_connected is False
