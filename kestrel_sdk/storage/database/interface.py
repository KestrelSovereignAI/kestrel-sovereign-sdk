"""Database backend ABC.

Async interface for relational backends used by sovereign and any
feature package that needs raw SQL access through the same connection
pool. The framework ships concrete `SQLiteBackend` and `PostgresBackend`
that satisfy this contract; feature packages depend only on the ABC.

All queries use SQLite-style `?` placeholders. PostgreSQL backends are
expected to translate to `$1, $2, ...` internally so feature code stays
backend-agnostic.
"""

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, List, Optional, Sequence, Tuple

Params = Sequence[Any]
Row = Tuple[Any, ...]


class DatabaseBackend(ABC):
    """Unified async interface for SQLite and PostgreSQL backends."""

    @property
    @abstractmethod
    def backend_type(self) -> str:
        """Return ``"sqlite"`` or ``"postgres"``."""
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Whether the underlying connection/pool is open."""
        ...

    @abstractmethod
    async def connect(self) -> None:
        """Establish the database connection or pool."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the connection or pool."""
        ...

    @abstractmethod
    async def execute(self, query: str, params: Params = ()) -> int:
        """Run a write query and return rows affected."""
        ...

    @abstractmethod
    async def execute_many(self, query: str, params_list: List[Params]) -> int:
        """Run a write query against many parameter sets and return total rows affected."""
        ...

    @abstractmethod
    async def fetch_one(self, query: str, params: Params = ()) -> Optional[Row]:
        """Return the first row, or ``None`` if the query produced no rows."""
        ...

    @abstractmethod
    async def fetch_all(self, query: str, params: Params = ()) -> List[Row]:
        """Return all rows produced by the query."""
        ...

    @abstractmethod
    async def fetch_val(self, query: str, params: Params = ()) -> Optional[Any]:
        """Return the first column of the first row, or ``None``."""
        ...

    @abstractmethod
    async def execute_script(self, script: str) -> None:
        """Run a multi-statement script (used for migrations / DDL)."""
        ...

    @asynccontextmanager
    @abstractmethod
    async def transaction(self) -> AsyncIterator[None]:
        """Transaction context manager — commits on success, rolls back on exception."""
        ...

    async def table_exists(self, table_name: str) -> bool:
        """Default table-existence check; backends may override for efficiency."""
        if self.backend_type == "sqlite":
            row = await self.fetch_one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            )
        else:
            row = await self.fetch_one(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=?",
                (table_name,),
            )
        return row is not None


class DatabaseError(Exception):
    """Base exception for database operations."""


class ConnectionError(DatabaseError):
    """Failed to connect to the database."""


class QueryError(DatabaseError):
    """Query execution failed."""


class TransactionError(DatabaseError):
    """Transaction operation failed."""
