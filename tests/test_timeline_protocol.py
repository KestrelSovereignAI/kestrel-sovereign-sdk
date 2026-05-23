"""
Tests for timeline protocols.

Verify that stub classes satisfying the protocol via duck typing pass
isinstance checks.
"""

from datetime import date, datetime

import pytest

from kestrel_sdk.timeline import EventProtocol, PersonProtocol, TimelineProtocol


class StubTimeline:
    """Stub timeline class for testing protocol conformance."""

    def __init__(self):
        self.id = "timeline-123"
        self.agent_did = "did:key:z6Mk..."
        self.subject_name = "Jane Doe"
        self.title = "Life Story of Jane Doe"
        self.coherence_score = 0.85
        self.created_at = datetime(2024, 1, 15, 10, 30, 0)


class StubEvent:
    """Stub event class for testing protocol conformance."""

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
    """Stub person class for testing protocol conformance."""

    def __init__(self):
        self.id = "person-789"
        self.timeline_id = "timeline-123"
        self.name = "John Smith"
        self.aliases = ["Johnny", "J. Smith"]
        self.relationship = "friend"


def test_timeline_protocol_conformance():
    """A stub class with required attributes satisfies TimelineProtocol."""
    timeline = StubTimeline()
    assert isinstance(timeline, TimelineProtocol)
    assert timeline.id == "timeline-123"
    assert timeline.agent_did == "did:key:z6Mk..."
    assert timeline.subject_name == "Jane Doe"
    assert timeline.title == "Life Story of Jane Doe"
    assert timeline.coherence_score == 0.85
    assert isinstance(timeline.created_at, datetime)


def test_timeline_protocol_optional_subject_name():
    """TimelineProtocol allows subject_name to be None."""
    timeline = StubTimeline()
    timeline.subject_name = None
    assert isinstance(timeline, TimelineProtocol)
    assert timeline.subject_name is None


def test_event_protocol_conformance():
    """A stub class with required attributes satisfies EventProtocol."""
    event = StubEvent()
    assert isinstance(event, EventProtocol)
    assert event.id == "event-456"
    assert event.timeline_id == "timeline-123"
    assert event.description == "Graduated from university"
    assert event.event_date == date(1985, 6, 15)
    assert event.date_precision == "exact"
    assert event.people == ["Jane Doe", "John Smith"]
    assert event.themes == ["education", "achievement"]
    assert event.sentiment == "joyful"


def test_event_protocol_optional_fields():
    """EventProtocol allows event_date and sentiment to be None."""
    event = StubEvent()
    event.event_date = None
    event.sentiment = None
    assert isinstance(event, EventProtocol)
    assert event.event_date is None
    assert event.sentiment is None


def test_person_protocol_conformance():
    """A stub class with required attributes satisfies PersonProtocol."""
    person = StubPerson()
    assert isinstance(person, PersonProtocol)
    assert person.id == "person-789"
    assert person.timeline_id == "timeline-123"
    assert person.name == "John Smith"
    assert person.aliases == ["Johnny", "J. Smith"]
    assert person.relationship == "friend"


def test_person_protocol_optional_relationship():
    """PersonProtocol allows relationship to be None."""
    person = StubPerson()
    person.relationship = None
    assert isinstance(person, PersonProtocol)
    assert person.relationship is None


def test_person_protocol_empty_aliases():
    """PersonProtocol allows empty aliases list."""
    person = StubPerson()
    person.aliases = []
    assert isinstance(person, PersonProtocol)
    assert person.aliases == []
