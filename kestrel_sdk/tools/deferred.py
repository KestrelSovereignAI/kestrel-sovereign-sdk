"""In-process render delivery after the host completes post-tool policy.

The public dictionary is the ordinary model-facing tool result. A private
factory may retain native results, but is never part of that dictionary or its
JSON representation. Only a host that implements this completion contract may
finalize the factory, after all post-tool hooks finish. This grants no tool or
HTTP authority and does not replace the host's audit boundary.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields
from inspect import iscoroutinefunction
from threading import Lock
from typing import Callable

from .result import ToolResult


@dataclass(frozen=True)
class FinalizedToolResult:
    result: dict
    parts: list[dict]
    withheld: bool


def _validate_envelope(result: dict) -> None:
    names = {item.name for item in fields(ToolResult)}
    ToolResult(**{name: value for name, value in result.items() if name in names})


class DeferredToolResult(dict):
    """A tool envelope with one private, synchronous render-parts factory.

    ``finalize(post_hook_result)`` consumes the factory even when policy
    replaced or changed the public envelope, in which case it is withheld.
    Existing parts retain their original host ordering; the host emits only
    the newly delivered parts after policy.

    Copies deliberately become plain dictionaries. A copied/serialized result
    cannot duplicate the private delivery capability. Feature packages must
    check host support before advertising a capability that requires delivery.
    """

    def __init__(self, result: dict, *, publish: Callable[[], list[dict]]):
        if not isinstance(result, dict):
            raise TypeError("Deferred tool result must be a dictionary")
        if (not callable(publish) or iscoroutinefunction(publish)
                or iscoroutinefunction(getattr(publish, "__call__", None))):
            raise TypeError("Deferred render delivery requires a synchronous callable")
        payload = deepcopy(dict(result))
        _validate_envelope(payload)
        super().__init__(payload)
        self._baseline = deepcopy(payload)
        self._publish = publish
        self._delivery_lock = Lock()

    def _take_factory(self):
        with self._delivery_lock:
            publish = self._publish
            if publish is None:
                raise RuntimeError("Deferred tool result was already finalized or transferred")
            self._publish = None
            return publish

    def for_tool(self, name: str, pending_parts: list[dict], *,
                 include_success: bool = False) -> DeferredToolResult:
        """Transfer delivery once while a tool wrapper adds its own metadata.

        This happens before post-tool policy. An already changed envelope is
        refused, never given a fresh baseline that would bypass redaction.
        """
        publish = self._take_factory()
        payload = deepcopy(dict(self))
        if payload != self._baseline:
            raise ValueError("Deferred tool result changed before wrapper metadata was added")
        if not isinstance(name, str) or not name:
            raise ValueError("Deferred tool wrapper requires a non-empty tool name")
        if type(include_success) is not bool:
            raise TypeError("Deferred tool wrapper success projection must be a boolean")
        ToolResult.ok("Buffered render parts", parts=pending_parts)
        payload["tool"] = name
        if include_success:
            payload["success"] = payload["status"] != "error"
        if pending_parts:
            payload["parts"] = [*deepcopy(pending_parts), *(payload.get("parts") or [])]
        return DeferredToolResult(payload, publish=publish)

    def finalize(self, post_hook_result: dict) -> FinalizedToolResult:
        """Consume once against the actual object returned by post-tool policy."""
        publish = self._take_factory()
        if not isinstance(post_hook_result, dict):
            raise TypeError("Post-tool policy must return a dictionary envelope")
        result = deepcopy(dict(post_hook_result))
        if post_hook_result is not self or result != self._baseline:
            return FinalizedToolResult(result, [], withheld=True)
        parts = deepcopy(publish())
        if parts is None:
            raise TypeError("Deferred render delivery must return a list of parts")
        # Keep structural validation canonical with normal ToolResult.parts;
        # wire sanitization and size limits still belong to the host.
        ToolResult.ok("Deferred render delivery", parts=parts)
        if parts:
            result["parts"] = [*(result.get("parts") or []), *deepcopy(parts)]
            _validate_envelope(result)
        return FinalizedToolResult(result, parts, withheld=False)

    def __copy__(self):
        return dict(self)

    def __deepcopy__(self, memo):
        return deepcopy(dict(self), memo)

    def __reduce_ex__(self, protocol):
        raise TypeError("Deferred render delivery cannot be pickled")

    def __reduce__(self):
        raise TypeError("Deferred render delivery cannot be pickled")

    def __getstate__(self):
        raise TypeError("Deferred render delivery cannot be pickled")
