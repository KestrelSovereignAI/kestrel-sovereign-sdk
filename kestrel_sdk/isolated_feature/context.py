"""Task-local access to trusted isolated-tool execution metadata."""

from __future__ import annotations

from contextvars import ContextVar
from threading import Event

from .protocol import ToolExecutionContext


class _ToolExecutionContextScope:
    """One invocation's context, shared with copied task/thread contexts.

    ``ContextVar`` values are copied when a handler creates an asyncio task or
    when ``asyncio.to_thread`` starts a worker.  The mutable active flag lets
    the dispatching task revoke access from every such copy when the RPC ends.
    ``Event`` makes that flag safe to read from a worker thread.
    """

    def __init__(self, context: ToolExecutionContext | None) -> None:
        self._context = context
        self._active = Event()
        self._active.set()

    def get(self) -> ToolExecutionContext | None:
        """Return the context only while its originating RPC remains active."""

        if not self._active.is_set():
            return None
        return self._context

    def invalidate(self) -> None:
        """Revoke the context from this and all copied execution contexts."""

        self._active.clear()


_active_tool_execution_context: ContextVar[_ToolExecutionContextScope | None] = ContextVar(
    "kestrel_isolated_tool_execution_context",
    default=None,
)


def get_tool_execution_context() -> ToolExecutionContext | None:
    """Return trusted metadata for the current isolated ``tools/call``, if any.

    The service sets this only while dispatching one RPC request and revokes it
    on success, failure, or cancellation. A handler-created task or worker
    thread inherits a reference to the invocation scope, but it sees ``None``
    after that scope ends. It is intentionally not derived from user-controlled
    tool arguments.
    """

    scope = _active_tool_execution_context.get()
    return None if scope is None else scope.get()
