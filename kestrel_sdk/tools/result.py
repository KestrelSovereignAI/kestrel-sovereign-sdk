"""
Tool result envelope contract.

Every Kestrel feature tool returns a :class:`ToolResult` so the
constitutional honesty layer can deterministically compare LLM
narration against the actual tool outcome. The framework's
feature-registry validator rejects features whose tools return any
other shape (issue #1042 layer 4).

Convention follows the SDK's existing pattern (``HookOutput``,
``StorageResult``, ``SignalResult``, ``GenerationResult``):
``@dataclass(frozen=True)`` with classmethod factory constructors. The
``__post_init__`` enforces the status / confirmation / error invariants
strictly at construction time — there is no permissive "just take
whatever they pass" path.

Usage:

    from kestrel_sdk.tools.result import ToolResult

    @tool(name="save_fact", ...)
    async def save_fact(self, fact: str) -> ToolResult:
        node_id = await self._memory.store(fact)
        if node_id is None:
            return ToolResult.failed(
                "save_fact: memory store returned no node_id; "
                "fact was not persisted",
            )
        return ToolResult.ok(
            confirmation=f"Saved fact {node_id[:8]}",
            data={"node_id": node_id},
        )

The ``status`` field is the load-bearing one for the honesty layer:
the ``ResponseAuditHook`` deterministic narration check (issue #1042
layer 3) reads it directly to detect contradictions between an LLM's
"Saved!" claim and an underlying ``status=ToolResultStatus.ERROR``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Dict, List, Optional


class ToolResultStatus(StrEnum):
    """Lifecycle states of a tool invocation outcome.

    Every consumer (LLM-facing serialization, log lines, audit records,
    the honesty hook's regex matcher) treats the status as a stable
    lowercase string token. ``StrEnum`` (Python 3.11+ stdlib) gives us
    ``str(status) == status.value`` and f-string interpolation that
    yields the bare wire token — unlike a plain ``(str, Enum)`` mix-in,
    where ``str(ToolResultStatus.OK)`` would render as
    ``"ToolResultStatus.OK"``. ``requires-python = ">=3.11,<3.15"`` in
    pyproject covers ``StrEnum`` availability.
    """

    OK = "ok"
    ERROR = "error"
    PARTIAL = "partial"


@dataclass(frozen=True)
class ToolResult:
    """The shape every Kestrel feature tool returns.

    Attributes:
        status: Outcome lifecycle state.
            ``OK`` means the tool's intended action completed and any
            user-facing claim of success ("Saved", "Done") is honest.
            ``ERROR`` means the action did not complete; ``error``
            describes what failed.
            ``PARTIAL`` means the action completed enough to produce a
            confirmation but with caveats — both ``confirmation`` and
            ``error`` are populated and the user-facing reply must
            surface BOTH (e.g. "Saved with degraded indexing"). The
            constitutional honesty layer (#1042) explicitly rejects
            replies that leave the ``error`` half of a PARTIAL result
            unspoken.
        confirmation: Human-readable success line. Required when
            ``status`` is ``OK`` or ``PARTIAL``; forbidden when
            ``status`` is ``ERROR``.
        error: Human-readable failure description. Required when
            ``status`` is ``ERROR`` or ``PARTIAL``; forbidden when
            ``status`` is ``OK``.
        data: Optional machine-readable payload (node ids, counts,
            paths, etc). Free-form because individual features need
            different shapes; the constitutional layer reads
            ``status`` / ``confirmation`` / ``error`` only.
        parts: Optional list of first-class typed render parts
            (kestrel-sovereign #2641). Each entry is a dict of the
            ``{type, data, id?}`` shape the framework's ``emit_part``
            produces; the tool wrapper serializes them onto the result
            envelope's ``parts`` field so they survive subagent
            dispatch by contract instead of ContextVar happenstance.
            Validation here is structural only (a list of dicts, each
            with a non-empty string ``type``); the framework applies
            its size/type wire sanitization at the dispatch boundary.

    Construction: prefer the classmethod factories
    (:meth:`ok`, :meth:`error`, :meth:`partial`) — they wrap the
    invariant checks and read more like English at the call site.

    Frozen + dataclass means: no silent mutation after construction,
    structurally hashable / comparable for tests, and consistent with
    every other "decision envelope" in the SDK (``HookOutput``).
    """

    status: ToolResultStatus
    confirmation: Optional[str] = None
    error: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    parts: Optional[List[Dict[str, Any]]] = None

    def __post_init__(self) -> None:
        # Type guard: status MUST be the enum, not a bare string.
        # We accept callers that pass the enum value ("ok") and
        # coerce — but only for the canonical lowercase values; any
        # other type is a programmer error.
        if not isinstance(self.status, ToolResultStatus):
            if isinstance(self.status, str):
                try:
                    coerced = ToolResultStatus(self.status)
                except ValueError as exc:
                    raise ValueError(
                        f"ToolResult.status must be a ToolResultStatus or "
                        f"one of {[s.value for s in ToolResultStatus]}, "
                        f"got {self.status!r}"
                    ) from exc
                # frozen=True means we can't assign self.status normally —
                # use object.__setattr__ as the conventional escape hatch
                # (also used by stdlib dataclasses themselves).
                object.__setattr__(self, "status", coerced)
            else:
                raise TypeError(
                    "ToolResult.status must be a ToolResultStatus, got "
                    f"{type(self.status).__name__}"
                )

        # Field types: confirmation/error are str|None; data is dict|None.
        if self.confirmation is not None and not isinstance(self.confirmation, str):
            raise TypeError(
                "ToolResult.confirmation must be a str or None, got "
                f"{type(self.confirmation).__name__}"
            )
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError(
                "ToolResult.error must be a str or None, got "
                f"{type(self.error).__name__}"
            )
        if self.data is not None and not isinstance(self.data, dict):
            raise TypeError(
                "ToolResult.data must be a dict or None, got "
                f"{type(self.data).__name__}"
            )
        # parts: structural validation only. Each entry must at least be
        # the ``{type: str, ...}`` shape the framework's part renderer
        # keys on — catching a wrong-shaped return at construction time,
        # where the feature author sees the traceback, instead of at the
        # dispatch boundary where it would be silently dropped. Deep wire
        # sanitization (size caps, control characters) deliberately stays
        # in the framework so there is one source of truth for the rules.
        if self.parts is not None:
            if not isinstance(self.parts, list):
                raise TypeError(
                    "ToolResult.parts must be a list of dicts or None, got "
                    f"{type(self.parts).__name__}"
                )
            for entry in self.parts:
                if not isinstance(entry, dict):
                    raise TypeError(
                        "ToolResult.parts entries must be dicts, got "
                        f"{type(entry).__name__}"
                    )
                part_type = entry.get("type")
                if not isinstance(part_type, str) or not part_type:
                    raise ValueError(
                        "ToolResult.parts entries require a non-empty string "
                        f"'type', got {part_type!r}"
                    )

        # Status-specific invariants.
        # - OK: must have confirmation, must NOT have error.
        # - ERROR: must have error, must NOT have confirmation.
        # - PARTIAL: must have BOTH confirmation and error.
        # These are load-bearing for the constitutional honesty layer:
        # without them, a feature could quietly emit
        # ``ToolResult(status=OK, error="actually failed")`` and the
        # narration check would pass.
        if self.status is ToolResultStatus.OK:
            if not self.confirmation:
                raise ValueError(
                    "ToolResult(status=OK) requires a non-empty confirmation"
                )
            if self.error is not None:
                raise ValueError(
                    "ToolResult(status=OK) cannot carry an error; "
                    "use status=ERROR or status=PARTIAL"
                )
        elif self.status is ToolResultStatus.ERROR:
            if not self.error:
                raise ValueError(
                    "ToolResult(status=ERROR) requires a non-empty error"
                )
            if self.confirmation is not None:
                raise ValueError(
                    "ToolResult(status=ERROR) cannot carry a confirmation; "
                    "use status=OK or status=PARTIAL"
                )
        elif self.status is ToolResultStatus.PARTIAL:
            if not self.confirmation:
                raise ValueError(
                    "ToolResult(status=PARTIAL) requires a non-empty confirmation"
                )
            if not self.error:
                raise ValueError(
                    "ToolResult(status=PARTIAL) requires a non-empty error"
                )

    # ------------------------------------------------------------------
    # Classmethod factories (preferred construction path)
    # ------------------------------------------------------------------

    @classmethod
    def ok(
        cls,
        confirmation: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        parts: Optional[List[Dict[str, Any]]] = None,
    ) -> "ToolResult":
        """Construct an OK result.

        Args:
            confirmation: Human-readable success line. Surfaces in
                the LLM's reply as ground truth.
            data: Optional machine-readable payload.
            parts: Optional first-class typed render parts
                (``{type, data, id?}`` dicts) carried on the result
                envelope.
        """
        return cls(
            status=ToolResultStatus.OK,
            confirmation=confirmation,
            data=data,
            parts=parts,
        )

    @classmethod
    def failed(
        cls,
        error: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        parts: Optional[List[Dict[str, Any]]] = None,
    ) -> "ToolResult":
        """Construct an ERROR result.

        Named ``failed`` (not ``error``) because a classmethod named
        ``error`` would shadow the dataclass ``error`` field at class
        attribute level — the dataclass-generated ``__init__`` reads
        ``cls.error`` lazily for the default, and would assign the
        classmethod into ``self.error`` when no error argument is
        passed. Past-tense ``failed`` mirrors ``ok`` and ``partial`` as
        result-state adjectives.

        Args:
            error: Human-readable failure description.
            data: Optional machine-readable payload (e.g. partial
                state captured before the error).
            parts: Optional first-class typed render parts — a
                failure can still carry e.g. the ``*_pending`` card
                it emitted before things went wrong.
        """
        return cls(
            status=ToolResultStatus.ERROR,
            error=error,
            data=data,
            parts=parts,
        )

    @classmethod
    def partial(
        cls,
        confirmation: str,
        error: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        parts: Optional[List[Dict[str, Any]]] = None,
    ) -> "ToolResult":
        """Construct a PARTIAL result.

        Use when an action completed enough to produce a confirmation
        but with caveats the LLM MUST surface to the user.

        Args:
            confirmation: Success line for the part that succeeded.
            error: Description of the caveat / partial failure.
            data: Optional machine-readable payload.
            parts: Optional first-class typed render parts
                (``{type, data, id?}`` dicts) carried on the result
                envelope.
        """
        return cls(
            status=ToolResultStatus.PARTIAL,
            confirmation=confirmation,
            error=error,
            data=data,
            parts=parts,
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation.

        Used by the framework when streaming tool results into the
        next LLM turn's message history. Omits fields that are
        ``None`` so the wire format doesn't carry noise.
        """
        out: Dict[str, Any] = {"status": self.status.value}
        if self.confirmation is not None:
            out["confirmation"] = self.confirmation
        if self.error is not None:
            out["error"] = self.error
        if self.data is not None:
            out["data"] = self.data
        # Empty list is omitted like None: "no parts" serializes to the
        # exact pre-parts envelope shape, byte for byte.
        if self.parts:
            out["parts"] = self.parts
        return out
