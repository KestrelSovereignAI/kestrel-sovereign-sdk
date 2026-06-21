"""
Waitable provider contract.

Every "wait" in the Kestrel tree is the same shape: poll some external
state until it reaches a terminal condition or a timeout expires, then
map the outcome onto a :class:`~kestrel_sdk.tools.result.ToolResult`. The
generic wait *engine* (in core) owns that loop — the cap, the poll
interval, the audit ``reason``, and the OK/PARTIAL/ERROR mapping. A
feature only needs to teach the engine two things about its kind of
handle: how to read the current status, and whether that status is
terminal.

This module is that teaching contract. A feature implements
:class:`Waitable` for its handle namespace (``"talon"``, ``"task"``,
``"ci"``, ...) and registers it; the engine resolves ``"talon:job_42"``
to the right provider by the ``kind`` prefix and drives the loop.

The contract lives in the SDK (not core) so an *external* feature
package can implement it without importing kestrel-sovereign core —
core must never be a dependency of a feature. This mirrors how
:class:`ToolResult` and the ``@tool`` decorator ship from the SDK.

Usage::

    from kestrel_sdk.tools.waitable import Outcome, WaitStatus, Waitable

    class TalonWaitable:
        kind = "talon"
        signal = "talon.job_complete"

        def __init__(self, coordinator):
            self._coordinator = coordinator

        async def poll(self, handle: str) -> WaitStatus:
            info = await self._coordinator.read_job(handle)
            if info is None:
                return WaitStatus(Outcome.FAILED, f"unknown job {handle}")
            status = info["status"]
            if status == "complete":
                return WaitStatus(Outcome.DONE, "job complete", data=info)
            if status in ("failed", "reject", "finished_unknown"):
                return WaitStatus(Outcome.FAILED, f"job {status}", data=info)
            return WaitStatus(Outcome.PENDING, f"job {status}", data=info)

The single ``poll`` method intentionally folds "read status" and
"classify status" together: the engine never needs them apart, and a
provider is free to structure its internals however it likes (a pure
``classify`` helper over an async ``resolve`` reads well and tests
cleanly, but that split is the provider's business, not the contract's).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Dict, Optional, Protocol, runtime_checkable


class Outcome(StrEnum):
    """Classification of a single poll of a waitable handle.

    This is the one terminal vocabulary that replaces the divergent
    per-feature sets (``complete``/``failed``/``reject``/
    ``finished_unknown`` in talon, ``completed``/``failed``/``canceled``
    in tasks, ``success``/``failure`` in CI). A provider maps its native
    status strings onto these four; the engine maps these four onto
    :class:`ToolResult`.

    ``StrEnum`` (stdlib, 3.11+) gives bare-token interpolation
    (``f"{Outcome.DONE}" == "done"``), matching ``ToolResultStatus``.
    """

    PENDING = "pending"
    """Not terminal — the engine keeps polling until cap/timeout."""

    DONE = "done"
    """Terminal success. Engine returns ``ToolResult.ok``."""

    FAILED = "failed"
    """Terminal failure. Engine returns ``ToolResult.failed``."""

    PARTIAL = "partial"
    """Terminal-but-caveated (e.g. completed with degraded output).

    Engine returns ``ToolResult.partial``. Distinct from a *timeout*:
    a timeout is the engine giving up on a still-``PENDING`` handle and
    is the engine's call, not the provider's. A provider returns
    ``PARTIAL`` only when the handle itself reached a mixed terminal
    state.
    """

    def is_terminal(self) -> bool:
        """True when the engine should stop polling."""
        return self is not Outcome.PENDING


@dataclass(frozen=True)
class WaitStatus:
    """A provider's verdict on one poll of a handle.

    Attributes:
        outcome: The classified :class:`Outcome`.
        summary: Human-readable one-liner ("job complete (rc=0)",
            "still running", "CI failing on 2 checks"). Flows into the
            engine's ``ToolResult.confirmation``/``error`` text so the
            agent — and the honesty layer — see ground truth.
        data: Optional machine-readable payload (return codes, log
            tails, artifact lists, the raw status). Free-form; the
            engine passes it through to ``ToolResult.data`` and never
            inspects it.

    Frozen dataclass, consistent with every other decision envelope in
    the SDK (``ToolResult``, ``HookOutput``).
    """

    outcome: Outcome
    summary: str
    data: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, Outcome):
            if isinstance(self.outcome, str):
                try:
                    coerced = Outcome(self.outcome)
                except ValueError as exc:
                    raise ValueError(
                        "WaitStatus.outcome must be an Outcome or one of "
                        f"{[o.value for o in Outcome]}, got {self.outcome!r}"
                    ) from exc
                object.__setattr__(self, "outcome", coerced)
            else:
                raise TypeError(
                    "WaitStatus.outcome must be an Outcome, got "
                    f"{type(self.outcome).__name__}"
                )
        if not self.summary or not isinstance(self.summary, str):
            raise ValueError("WaitStatus.summary must be a non-empty str")
        if self.data is not None and not isinstance(self.data, dict):
            raise TypeError(
                "WaitStatus.data must be a dict or None, got "
                f"{type(self.data).__name__}"
            )


@runtime_checkable
class Waitable(Protocol):
    """A provider that teaches the generic wait engine about one handle kind.

    A feature implements this for its namespace and registers it (via
    the Feature registry / entry-point group). The engine dispatches a
    ``"<kind>:<handle>"`` reference to the provider whose :attr:`kind`
    matches the prefix, then drives the poll loop with :meth:`poll`.

    Attributes:
        kind: The handle namespace this provider owns (``"talon"``,
            ``"task"``, ``"ci"``). The token before the first ``:`` in a
            wait reference. Must be unique across registered providers.
        signal: The signal name emitted when a handle of this kind
            reaches a terminal state, or ``None`` if this kind has no
            signal-resume path. Used by the generic reconciler cron
            (``mode="signal"``) so a long wait can return immediately
            and be woken by ``wait.complete`` instead of holding a turn.

    Structural (``Protocol``) rather than an ABC so external packages
    need only match the shape — no inheritance coupling to the SDK.
    """

    kind: ClassVar[str]
    signal: ClassVar[Optional[str]]

    async def poll(self, handle: str) -> WaitStatus:
        """Read and classify the current status of ``handle``.

        ``handle`` is the part *after* the ``kind:`` prefix (the engine
        strips it). Returns a :class:`WaitStatus`; the engine stops when
        ``outcome.is_terminal()`` and otherwise sleeps and polls again.

        Must not sleep or loop — one call is one observation. Looping,
        timing, and backoff are the engine's responsibility.
        """
        ...


@runtime_checkable
class MonitorableWaitable(Waitable, Protocol):
    """A :class:`Waitable` that can enumerate its own in-flight handles.

    The principle: *every async waitable should be wakeable*. A blocking
    ``wait("talon:job")`` already polls a single handle; a monitorable
    provider additionally tells the generic reconciler cron which handles
    are currently in flight, so the reconciler can wake the agent on ANY
    of them reaching a terminal state — without the agent having held a
    turn or even explicitly asked to wait. This generalizes what a
    per-feature monitor (e.g. talon's old ``talon_monitor``) did for one
    kind to every async provider.

    Implementing this is optional: a provider that only supports explicit
    blocking waits omits it, and the reconciler simply skips enumeration
    for that kind. The reconciler detects support structurally
    (``isinstance(provider, MonitorableWaitable)`` /
    ``hasattr(provider, "active_handles")``), so poll-only providers stay
    valid against the base :class:`Waitable`.

    A provider that declares :attr:`Waitable.signal` should generally be
    monitorable too — otherwise nothing drives the signal-resume path for
    handles the agent didn't explicitly block on.
    """

    async def active_handles(self) -> list[str]:
        """Return the handles currently in flight for this kind.

        These are the non-terminal handles the reconciler should poll and
        potentially emit a completion signal for (the part after the
        ``kind:`` prefix — e.g. talon job ids for dispatched-but-unfinished
        jobs). Return an empty list when nothing is in flight.

        Should be cheap and side-effect-free beyond reading durable state;
        the reconciler calls it every cron tick. Classifying and signaling
        are the reconciler's job — this only enumerates.
        """
        ...
