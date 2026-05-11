"""Shared models for channel adapter packages."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MessageDirection(str, Enum):
    """Direction of a channel message."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class DeliveryStatus(str, Enum):
    """Result state for a channel send attempt."""

    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"


@dataclass
class ChannelMessage:
    """Inbound or outbound message carried by a channel adapter."""

    channel_type: str
    direction: MessageDirection
    sender: str
    recipient: str
    content: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)
    agent_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a stable JSON-shaped dictionary."""

        return {
            "id": self.id,
            "channel_type": self.channel_type,
            "direction": self.direction.value,
            "sender": self.sender,
            "recipient": self.recipient,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "agent_id": self.agent_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelMessage":
        """Deserialize from a dictionary, ignoring unknown keys."""

        payload = dict(data)
        if not isinstance(payload.get("direction"), MessageDirection):
            payload["direction"] = MessageDirection(payload["direction"])
        timestamp = payload.get("timestamp")
        if isinstance(timestamp, str):
            payload["timestamp"] = datetime.fromisoformat(timestamp)
        fields = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in payload.items() if key in fields})


@dataclass
class DeliveryReceipt:
    """Result of sending a message through a channel adapter."""

    message_id: str
    status: DeliveryStatus
    channel_type: str
    error: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a stable JSON-shaped dictionary."""

        return {
            "message_id": self.message_id,
            "status": self.status.value,
            "channel_type": self.channel_type,
            "error": self.error,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class ChannelConfig:
    """Per-agent configuration for one messaging channel."""

    channel_type: str
    agent_id: str = ""
    enabled: bool = True
    api_key: str | None = None
    allowed_senders: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def is_sender_allowed(self, sender: str) -> bool:
        """Return whether ``sender`` may send inbound messages."""

        return not self.allowed_senders or sender in self.allowed_senders

    def to_dict(self) -> dict[str, Any]:
        """Serialize without exposing secret material."""

        data = asdict(self)
        data.pop("api_key", None)
        data["has_api_key"] = self.api_key is not None
        return data


MessageCallback = Callable[[ChannelMessage], Awaitable[None]]
