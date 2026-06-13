"""Kestrel SDK — relational database surface for feature packages.

Splits the framework's existing concrete `SQLiteBackend` / `PostgresBackend`
implementations from the contract that feature packages develop against:

* `DatabaseBackend` — async ABC for execute/fetch/transaction (sqlite-style ?
  placeholders, the backend rewrites for PostgreSQL).
* `PrivacyMode` — the canonical 6-mode enum used across the agent.
* `EngineTarget` + `resolve_engine_target` — translates a privacy mode into
  the concrete SQLAlchemy URL a feature package should bind its ORM engine to.

The SDK deliberately stays free of SQLAlchemy and aiosqlite; entity packages
bring their own ORM. This module is pure-stdlib.
"""

from .interface import (
    ConnectionError,
    DatabaseBackend,
    DatabaseError,
    Params,
    QueryError,
    Row,
    TransactionError,
)
from .privacy import EngineTarget, PrivacyMode, resolve_engine_target

__all__ = [
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
]
