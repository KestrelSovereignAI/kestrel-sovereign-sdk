"""Provider protocol for outbound delivery packages."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import DeliveryResult, DeliveryTask


@runtime_checkable
class DeliveryProvider(Protocol):
    """A concrete sender for one or more outbound delivery channels."""

    @property
    def provider_name(self) -> str:
        """Stable provider identifier, such as ``resend`` or ``twilio``."""

    def supports_channel(self, channel_type: str) -> bool:
        """Return whether this provider can deliver ``channel_type``."""

    async def deliver(self, task: DeliveryTask) -> DeliveryResult:
        """Attempt to deliver a task and return the provider result."""
