"""Channel adapter contracts for messaging integrations.

Channel packages implement these SDK contracts and register their adapters
with the framework through the ``kestrel_sovereign.channel_adapters`` entry
point group.
"""

from .base import ChannelAdapter
from .models import (
    ChannelConfig,
    ChannelMessage,
    DeliveryReceipt,
    DeliveryStatus,
    MessageCallback,
    MessageDirection,
)

CHANNEL_ADAPTER_ENTRY_POINT_GROUP = "kestrel_sovereign.channel_adapters"

__all__ = [
    "CHANNEL_ADAPTER_ENTRY_POINT_GROUP",
    "ChannelAdapter",
    "ChannelConfig",
    "ChannelMessage",
    "DeliveryReceipt",
    "DeliveryStatus",
    "MessageCallback",
    "MessageDirection",
]
