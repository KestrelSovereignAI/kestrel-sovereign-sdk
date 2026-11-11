"""Host ownership of execution errors and their public projection.

A host can run SDK features inside execution custody: it raises its own
authority, commit and retirement errors through the tool methods it calls.
Those are control facts, not tool failures. The SDK tool wrapper converts an
ordinary exception into a failed result, which would erase such a fact at the
inner boundary and make the outcome depend on which wrapper ran (#105).

The host owns the answer to "is this control?". It registers one predicate
for the process, and SDK tool/native HTTP boundaries re-raise the original exception,
unchanged, whenever that predicate says so. The SDK never decides what counts
as control: with nothing registered, no exception is control and standalone
behaviour is unchanged.

Native HTTP error projection uses the same predicate before converting unary
or streaming failures to public protocol errors. It does not serialize private
host evidence or make bare cancellation a host control error. An embedding
still owns its transport, original invocation and cleanup.

A durable-effect host also owns ordinary error classification: only its
original journal can distinguish a failed operation from an uncertain effect.
Feature tools snapshot that contract before running their bodies and preserve
the original exception until that boundary has classified it.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

ControlErrorPredicate = Callable[[BaseException], bool]


def host_owns_tool_error_boundary(host: object) -> bool:
    """Whether the host promises durable classification before projection.

    This capability describes error ownership, never execution authority.
    Callers capture it before execution so withdrawing a capability while a
    tool runs cannot erase that tool's original error.
    """
    return getattr(host, "supports_durable_tool_effects", None) is True

_registration_lock = threading.Lock()
_control_error_predicate: Optional[ControlErrorPredicate] = None


def register_control_error_predicate(fn: ControlErrorPredicate) -> None:
    """Install the host's predicate for execution-control errors.

    Registration is process-global. Registering the same function again is a
    no-op; a different function raises :class:`RuntimeError` and leaves the
    installed predicate in place, because two hosts disagreeing about what is
    control in one process has no safe resolution.

    Raises:
        TypeError: ``fn`` is not callable.
        RuntimeError: A different predicate is already registered.
    """
    if not callable(fn):
        raise TypeError("A control-error predicate must be callable")
    global _control_error_predicate
    with _registration_lock:
        if _control_error_predicate is fn:
            return
        if _control_error_predicate is not None:
            raise RuntimeError(
                "A different control-error predicate is already registered; "
                "the host owns exactly one per process"
            )
        _control_error_predicate = fn


def is_host_control_error(exc: BaseException) -> bool:
    """Return whether the registered host predicate classifies ``exc`` as control.

    ``False`` when no predicate is registered. A predicate that raises
    propagates its error, and one that returns anything but a ``bool`` raises
    :class:`TypeError`, so a broken host classification fails loudly rather
    than silently erasing or inventing a control fact.
    """
    predicate = _control_error_predicate
    if predicate is None:
        return False
    verdict = predicate(exc)
    if type(verdict) is not bool:
        raise TypeError("A control-error predicate must return a boolean")
    return verdict
