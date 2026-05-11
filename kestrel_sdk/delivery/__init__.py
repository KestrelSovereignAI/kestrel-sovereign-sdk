"""Delivery provider contracts for outbound integrations."""

from .base import DeliveryProvider
from .models import DeliveryResult, DeliveryTask

DELIVERY_PROVIDER_ENTRY_POINT_GROUP = "kestrel_sovereign.delivery_providers"

__all__ = [
    "DELIVERY_PROVIDER_ENTRY_POINT_GROUP",
    "DeliveryProvider",
    "DeliveryResult",
    "DeliveryTask",
]
