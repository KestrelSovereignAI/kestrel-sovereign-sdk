"""Provider-neutral output event envelopes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OutputKind(str, Enum):
    """High-level outbound work categories."""

    CHANNEL_MESSAGE = "channel_message"
    DELIVERY_TASK = "delivery_task"
    WORKFLOW_EVENT = "workflow_event"
    TASK_UPDATE = "task_update"


@dataclass
class OutputDestination:
    """Where an output event should be delivered."""

    channel_type: str
    recipient: str
    provider: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-shaped dictionary."""

        return {
            "channel_type": self.channel_type,
            "recipient": self.recipient,
            "provider": self.provider,
            "metadata": self.metadata,
        }


@dataclass
class OutputEvent:
    """Provider-neutral outbound event envelope."""

    kind: OutputKind
    payload: dict[str, Any]
    destination: OutputDestination | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-shaped dictionary."""

        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "kind": self.kind.value,
            "payload": self.payload,
            "destination": (
                self.destination.to_dict() if self.destination is not None else None
            ),
            "metadata": self.metadata,
        }
