"""
Timeline sharing protocols for cross-system serialization.

Pluggable serialization formats for timeline sharing (internal portal JSON,
FHIR, IPFS-CAR, etc.). Feature packages can implement custom serializers
for different sharing contexts.
"""

import json
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TimelineSharingProtocol(Protocol):
    """
    Protocol for pluggable timeline serialization.

    Implementations can serialize timelines to different formats (JSON, FHIR,
    IPFS-CAR, etc.) for cross-system sharing.
    """

    @property
    def content_type(self) -> str:
        """MIME type of the serialized output (e.g., 'application/json')."""
        ...

    def serialize(
        self,
        timeline: Any,
        events: list[Any],
        people: list[Any],
    ) -> bytes:
        """
        Serialize timeline, events, and people to bytes.

        Args:
            timeline: Object conforming to TimelineProtocol
            events: List of objects conforming to EventProtocol
            people: List of objects conforming to PersonProtocol

        Returns:
            Serialized bytes in the format specified by content_type
        """
        ...


class JSONTimelineSerializer:
    """
    Default JSON serializer for timelines.

    Serializes timeline data to UTF-8 JSON. This is the default sharing
    transport for internal portal use.
    """

    content_type = "application/json"

    def serialize(
        self,
        timeline: Any,
        events: list[Any],
        people: list[Any],
    ) -> bytes:
        """
        Serialize timeline, events, and people to JSON bytes.

        Uses Protocol attributes only — works with any object that has the
        required fields.

        Args:
            timeline: Object with TimelineProtocol attributes
            events: List of objects with EventProtocol attributes
            people: List of objects with PersonProtocol attributes

        Returns:
            UTF-8 encoded JSON bytes
        """
        data = {
            "timeline": {
                "id": timeline.id,
                "agent_did": timeline.agent_did,
                "subject_name": timeline.subject_name,
                "title": timeline.title,
                "coherence_score": timeline.coherence_score,
                "created_at": self._serialize_datetime(timeline.created_at),
            },
            "events": [
                {
                    "id": event.id,
                    "timeline_id": event.timeline_id,
                    "description": event.description,
                    "event_date": self._serialize_date(event.event_date),
                    "date_precision": event.date_precision,
                    "people": event.people,
                    "themes": event.themes,
                    "sentiment": event.sentiment,
                }
                for event in events
            ],
            "people": [
                {
                    "id": person.id,
                    "timeline_id": person.timeline_id,
                    "name": person.name,
                    "aliases": person.aliases,
                    "relationship": person.relationship,
                }
                for person in people
            ],
        }
        return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

    @staticmethod
    def _serialize_datetime(dt: datetime) -> str:
        """Serialize datetime to ISO format string."""
        return dt.isoformat()

    @staticmethod
    def _serialize_date(d: date | None) -> str | None:
        """Serialize date to ISO format string, or None."""
        return d.isoformat() if d is not None else None
