"""In-process render delivery after the host completes post-tool policy.

The public dictionary is the ordinary model-facing tool result. A private
factory may retain native results, but is never part of that dictionary or its
JSON representation. Only a host that implements this completion contract may
finalize the factory, after all post-tool hooks finish. This grants no tool or
HTTP authority and does not replace the host's audit boundary.
"""

from __future__ import annotations

from copy import deepcopy
from inspect import iscoroutinefunction
from typing import Callable

from .result import ToolResult


class DeferredToolResult(dict):
    """A tool envelope with one private, synchronous render-parts factory.

    ``finalize()`` consumes the factory even when policy changed the public
    envelope, in which case it is withheld. It returns a plain result mapping
    and the newly delivered parts. Existing parts retain their original host
    ordering; the host emits only the newly delivered parts after policy.

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
        super().__init__(deepcopy(result))
        self._baseline = deepcopy(result)
        self._publish = publish

    def finalize(self) -> tuple[dict, list[dict]]:
        """Consume this delivery once, after the host's post-tool hooks."""
        publish = self._publish
        if publish is None:
            raise RuntimeError("Deferred tool result was already finalized")
        self._publish = None
        result = deepcopy(dict(self))
        if result != self._baseline:
            return result, []
        parts = publish()
        if parts is None:
            raise TypeError("Deferred render delivery must return a list of parts")
        # Keep structural validation canonical with normal ToolResult.parts;
        # wire sanitization and size limits still belong to the host.
        ToolResult.ok("Deferred render delivery", parts=parts)
        parts = deepcopy(parts)
        if parts:
            result["parts"] = [*(result.get("parts") or []), *parts]
        return result, parts

    def __copy__(self):
        return dict(self)

    def __deepcopy__(self, memo):
        return deepcopy(dict(self), memo)

    def __reduce_ex__(self, protocol):
        raise TypeError("Deferred render delivery cannot be pickled")
