"""
Timeline Protocol — minimal duck-typed shape for timeline implementations.

These protocols define the minimal interface any timeline implementation must
conform to. They use Protocol with @runtime_checkable so implementations can
satisfy the interface via structural typing (duck typing) without inheritance.
"""

from datetime import date, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class TimelineProtocol(Protocol):
    """
    Minimal timeline interface for cross-implementation interop.

    Any object with these attributes can be used as a timeline across
    feature packages.
    """

    id: str
    agent_did: str
    subject_name: str | None  # whose story it is (use this, not product-specific terms)
    title: str
    coherence_score: float
    created_at: datetime


@runtime_checkable
class EventProtocol(Protocol):
    """
    Minimal event interface for timeline events.

    Any object with these attributes can be used as a timeline event.
    """

    id: str
    timeline_id: str
    description: str
    event_date: date | None
    date_precision: str  # "exact" | "year" | "decade" | "period"
    people: list[str]
    themes: list[str]
    sentiment: str | None


@runtime_checkable
class PersonProtocol(Protocol):
    """
    Minimal person interface for timeline people.

    Any object with these attributes can be used as a timeline person.
    """

    id: str
    timeline_id: str
    name: str
    aliases: list[str]
    relationship: str | None
