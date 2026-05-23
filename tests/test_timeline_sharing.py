"""
Tests for timeline sharing protocols.

Verify JSON serializer produces valid UTF-8 JSON and round-trips correctly.
"""

import json
from datetime import date, datetime

import pytest

from kestrel_sdk.timeline import (
    JSONTimelineSerializer,
    TimelineSharingProtocol,
)


class StubTimeline:
    """Stub timeline for serialization testing."""

    def __init__(self):
        self.id = "timeline-123"
        self.agent_did = "did:key:z6Mk..."
        self.subject_name = "Jane Doe"
        self.title = "Life Story of Jane Doe"
        self.coherence_score = 0.85
        self.created_at = datetime(2024, 1, 15, 10, 30, 0)


class StubEvent:
    """Stub event for serialization testing."""

    def __init__(self):
        self.id = "event-456"
        self.timeline_id = "timeline-123"
        self.description = "Graduated from university"
        self.event_date = date(1985, 6, 15)
        self.date_precision = "exact"
        self.people = ["Jane Doe", "John Smith"]
        self.themes = ["education", "achievement"]
        self.sentiment = "joyful"


class StubPerson:
    """Stub person for serialization testing."""

    def __init__(self):
        self.id = "person-789"
        self.timeline_id = "timeline-123"
        self.name = "John Smith"
        self.aliases = ["Johnny", "J. Smith"]
        self.relationship = "friend"


def test_json_serializer_is_protocol():
    """JSONTimelineSerializer satisfies TimelineSharingProtocol."""
    serializer = JSONTimelineSerializer()
    assert isinstance(serializer, TimelineSharingProtocol)


def test_json_serializer_content_type():
    """JSONTimelineSerializer has correct content type."""
    serializer = JSONTimelineSerializer()
    assert serializer.content_type == "application/json"


def test_json_serializer_produces_valid_json():
    """JSONTimelineSerializer produces valid UTF-8 JSON."""
    serializer = JSONTimelineSerializer()
    timeline = StubTimeline()
    events = [StubEvent()]
    people = [StubPerson()]

    result = serializer.serialize(timeline, events, people)

    # Should be bytes
    assert isinstance(result, bytes)

    # Should be valid UTF-8
    text = result.decode("utf-8")
    assert isinstance(text, str)

    # Should be valid JSON
    data = json.loads(text)
    assert isinstance(data, dict)


def test_json_serializer_round_trip():
    """JSONTimelineSerializer round-trips data correctly."""
    serializer = JSONTimelineSerializer()
    timeline = StubTimeline()
    events = [StubEvent()]
    people = [StubPerson()]

    result = serializer.serialize(timeline, events, people)
    data = json.loads(result.decode("utf-8"))

    # Verify timeline data
    assert data["timeline"]["id"] == "timeline-123"
    assert data["timeline"]["agent_did"] == "did:key:z6Mk..."
    assert data["timeline"]["subject_name"] == "Jane Doe"
    assert data["timeline"]["title"] == "Life Story of Jane Doe"
    assert data["timeline"]["coherence_score"] == 0.85
    assert data["timeline"]["created_at"] == "2024-01-15T10:30:00"

    # Verify events data
    assert len(data["events"]) == 1
    event = data["events"][0]
    assert event["id"] == "event-456"
    assert event["timeline_id"] == "timeline-123"
    assert event["description"] == "Graduated from university"
    assert event["event_date"] == "1985-06-15"
    assert event["date_precision"] == "exact"
    assert event["people"] == ["Jane Doe", "John Smith"]
    assert event["themes"] == ["education", "achievement"]
    assert event["sentiment"] == "joyful"

    # Verify people data
    assert len(data["people"]) == 1
    person = data["people"][0]
    assert person["id"] == "person-789"
    assert person["timeline_id"] == "timeline-123"
    assert person["name"] == "John Smith"
    assert person["aliases"] == ["Johnny", "J. Smith"]
    assert person["relationship"] == "friend"


def test_json_serializer_handles_null_fields():
    """JSONTimelineSerializer handles None values correctly."""
    serializer = JSONTimelineSerializer()

    # Timeline with null subject_name
    timeline = StubTimeline()
    timeline.subject_name = None

    # Event with null event_date and sentiment
    event = StubEvent()
    event.event_date = None
    event.sentiment = None

    # Person with null relationship
    person = StubPerson()
    person.relationship = None

    result = serializer.serialize(timeline, [event], [person])
    data = json.loads(result.decode("utf-8"))

    assert data["timeline"]["subject_name"] is None
    assert data["events"][0]["event_date"] is None
    assert data["events"][0]["sentiment"] is None
    assert data["people"][0]["relationship"] is None


def test_json_serializer_handles_empty_lists():
    """JSONTimelineSerializer handles empty lists correctly."""
    serializer = JSONTimelineSerializer()
    timeline = StubTimeline()

    # Empty events and people
    result = serializer.serialize(timeline, [], [])
    data = json.loads(result.decode("utf-8"))

    assert data["events"] == []
    assert data["people"] == []


def test_json_serializer_handles_unicode():
    """JSONTimelineSerializer handles Unicode characters correctly."""
    serializer = JSONTimelineSerializer()
    timeline = StubTimeline()
    timeline.subject_name = "José García"
    timeline.title = "生活故事"  # "Life Story" in Chinese

    event = StubEvent()
    event.description = "Café meeting with François"

    person = StubPerson()
    person.name = "François Müller"

    result = serializer.serialize(timeline, [event], [person])
    data = json.loads(result.decode("utf-8"))

    assert data["timeline"]["subject_name"] == "José García"
    assert data["timeline"]["title"] == "生活故事"
    assert data["events"][0]["description"] == "Café meeting with François"
    assert data["people"][0]["name"] == "François Müller"
