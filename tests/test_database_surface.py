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


def test_resolve_ephemeral_ignores_fallback():
    target = resolve_engine_target(PrivacyMode.EPHEMERAL, "postgresql://prod/db")
    assert target.url == "sqlite+aiosqlite:///:memory:"
    assert target.persistent is False
    assert "ephemeral" in target.description


def test_resolve_isolated_creates_tempfile_and_is_volatile():
    target = resolve_engine_target(PrivacyMode.ISOLATED, "postgresql://prod/db")
    assert target.url.startswith("sqlite+aiosqlite:///")
    assert target.persistent is False
    path = target.url.removeprefix("sqlite+aiosqlite:///")
    assert os.path.exists(path)
    os.unlink(path)


@pytest.mark.parametrize(
    "mode", [PrivacyMode.ANONYMOUS, PrivacyMode.NORMAL, PrivacyMode.PUBLIC]
)
def test_resolve_persistent_modes_pass_fallback_through(mode):
    target = resolve_engine_target(mode, "postgresql+asyncpg://u:p@h/db")
    assert target.url == "postgresql+asyncpg://u:p@h/db"
    assert target.persistent is True
    assert mode.value in target.description


@pytest.mark.parametrize(
    "mode", [PrivacyMode.ANONYMOUS, PrivacyMode.NORMAL, PrivacyMode.PUBLIC]
)
def test_resolve_persistent_requires_fallback(mode):
    with pytest.raises(ValueError, match="fallback_url"):
        resolve_engine_target(mode, "")


def test_engine_target_is_frozen():
    target = resolve_engine_target(PrivacyMode.EPHEMERAL, "")
    assert isinstance(target, EngineTarget)
    with pytest.raises(Exception):
        target.url = "x"  # type: ignore[misc]
