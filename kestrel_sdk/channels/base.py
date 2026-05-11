"""Abstract base class for channel adapter packages."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import ChannelConfig, DeliveryReceipt, MessageCallback


class ChannelAdapter(ABC):
    """Pluggable messaging channel adapter contract."""

    def __init__(self, config: ChannelConfig | None = None):
        self._config = config

    @property
    def config(self) -> ChannelConfig | None:
        """Return the current channel configuration."""

        return self._config

    @abstractmethod
    async def connect(self) -> None:
        """Establish an idempotent connection to the channel service."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Cleanly and idempotently disconnect from the channel service."""

    @abstractmethod
    async def send_message(self, to: str, content: str, **kwargs) -> DeliveryReceipt:
        """Send ``content`` to a channel-specific recipient identifier."""

    @abstractmethod
    async def on_message(self, callback: MessageCallback) -> None:
        """Register a callback for inbound channel messages."""

    @property
    @abstractmethod
    def channel_type(self) -> str:
        """Unique channel identifier, such as ``telegram`` or ``slack``."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Return whether the adapter has an active connection."""
