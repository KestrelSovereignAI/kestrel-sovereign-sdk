"""
Kestrel Sovereign SDK — lightweight interfaces for feature packages.

This package contains only the abstract base classes, protocols, and data models
that feature packages need to develop against. It has minimal dependencies
(pydantic only) so feature developers don't need to install the full
kestrel-sovereign framework.

Usage:
    from kestrel_sdk.features.base import Feature, tool
    from kestrel_sdk.hooks.base import Hook, HookEvent
    from kestrel_sdk.tools.base import AgentTool, ToolSchema
    from kestrel_sdk.timeline import TimelineProtocol, EventProtocol, PersonProtocol
"""

from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("kestrel-sovereign-sdk")
except PackageNotFoundError:
    __version__ = "0.0.0+local"

# Timeline protocols for cross-implementation interop
from kestrel_sdk.timeline import (
    EventProtocol,
    JSONTimelineSerializer,
    PersonProtocol,
    TimelineProtocol,
    TimelineSharingProtocol,
    VectorSearchBackend,
)

__all__ = [
    "__version__",
    "TimelineProtocol",
    "EventProtocol",
    "PersonProtocol",
    "TimelineSharingProtocol",
    "JSONTimelineSerializer",
    "VectorSearchBackend",
]
