"""
Timeline protocols for cross-implementation interop.

Minimal duck-typed shapes any timeline implementation must conform to.
Feature packages can implement these protocols without inheriting from
kestrel-feature-entities.
"""

from kestrel_sdk.timeline.protocol import (
    EventProtocol,
    PersonProtocol,
    TimelineProtocol,
)
from kestrel_sdk.timeline.sharing import (
    JSONTimelineSerializer,
    TimelineSharingProtocol,
)
from kestrel_sdk.timeline.vector_search import VectorSearchBackend

__all__ = [
    "TimelineProtocol",
    "EventProtocol",
    "PersonProtocol",
    "TimelineSharingProtocol",
    "JSONTimelineSerializer",
    "VectorSearchBackend",
]
