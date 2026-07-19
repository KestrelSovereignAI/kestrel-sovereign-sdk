"""Pending typed-parts buffer for the tool call currently executing.

First-class typed render parts (``selfie_pending``, ``selfie_finished``, todo
cards, citations, …) travel on the tool-result envelope's ``parts`` field
(kestrel-sovereign #2641): a tool either returns them explicitly via
``ToolResult(..., parts=[...])`` or emits them imperatively mid-execution
through the framework's ``emit_part`` API. The imperative path needs somewhere
to land when no per-turn collector is reachable — e.g. the tool runs on a
transport-spawned task whose frozen context predates the turn — and that
somewhere is this buffer.

The contract has two halves:

- The tool wrapper (``DynamicTool.execute`` in ``features/base.py`` — both the
  SDK's and the framework's) binds :func:`tool_result_parts_buffer` around the
  wrapped ``@tool`` call and attaches whatever accumulated to the serialized
  envelope's ``parts`` field.
- The framework's ``emit_part``, finding no turn collector bound, appends to
  :func:`current_tool_result_parts` instead of dropping the part.

The ContextVar lives HERE, in the SDK, so both wrappers and the framework
share one buffer regardless of which ``Feature`` base a feature subclasses.
The SDK performs no size/type sanitization on entries — that stays the
framework's job at its serialization/dispatch boundary, keeping a single
source of truth for the wire rules.
"""

from __future__ import annotations

import contextlib
import contextvars
from typing import Dict, Iterator, List, Optional

_pending_tool_parts: contextvars.ContextVar[Optional[List[Dict]]] = contextvars.ContextVar(
    "kestrel_pending_tool_parts", default=None,
)


@contextlib.contextmanager
def tool_result_parts_buffer() -> Iterator[List[Dict]]:
    """Bind a fresh pending-parts buffer for one tool execution.

    Entered by the tool wrapper around the wrapped ``@tool`` call; yields the
    buffer list so the wrapper can attach its contents to the result envelope
    after the call returns (or raises — parts emitted before a failure still
    travel). Bindings nest: an inner tool execution gets its own buffer and
    the outer one is restored on exit.
    """
    buffer: List[Dict] = []
    token = _pending_tool_parts.set(buffer)
    try:
        yield buffer
    finally:
        _pending_tool_parts.reset(token)


def current_tool_result_parts() -> Optional[List[Dict]]:
    """Return the pending-parts buffer of the tool call currently executing.

    ``None`` when no tool wrapper has bound a buffer in the current context —
    callers treat that as "no envelope under construction" and fall back to
    their own no-op behaviour.
    """
    return _pending_tool_parts.get()
