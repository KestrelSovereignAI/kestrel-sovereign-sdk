"""Shared models for durable outbound delivery providers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeliveryTask:
    """Provider-agnostic outbound delivery request."""

    channel_type: str
    recipient: str
    content: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-shaped dictionary."""

        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "channel_type": self.channel_type,
            "recipient": self.recipient,
            "content": self.content,
            "metadata": self.metadata,
        }


@dataclass
class DeliveryResult:
    """Result returned by a delivery provider."""

    success: bool
    error: str | None = None
    provider_message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-shaped dictionary."""

        data: dict[str, Any] = {"success": self.success}
        if self.error is not None:
            data["error"] = self.error
        if self.provider_message_id is not None:
            data["provider_message_id"] = self.provider_message_id
        if self.metadata:
            data["metadata"] = self.metadata
        return data
