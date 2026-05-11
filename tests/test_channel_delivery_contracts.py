from __future__ import annotations

import asyncio

from kestrel_sdk.channels import (
    CHANNEL_ADAPTER_ENTRY_POINT_GROUP,
    ChannelAdapter,
    ChannelConfig,
    ChannelMessage,
    DeliveryReceipt,
    DeliveryStatus,
    MessageDirection,
)
from kestrel_sdk.delivery import (
    DELIVERY_PROVIDER_ENTRY_POINT_GROUP,
    DeliveryProvider,
    DeliveryResult,
    DeliveryTask,
)
from kestrel_sdk.outputs import OutputDestination, OutputEvent, OutputKind


class EchoChannel(ChannelAdapter):
    def __init__(self, config: ChannelConfig | None = None):
        super().__init__(config)
        self._connected = False
        self.callbacks = []

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def send_message(self, to: str, content: str, **kwargs) -> DeliveryReceipt:
        return DeliveryReceipt(
            message_id=f"echo:{to}",
            status=DeliveryStatus.SUCCESS,
            channel_type=self.channel_type,
        )

    async def on_message(self, callback) -> None:
        self.callbacks.append(callback)

    @property
    def channel_type(self) -> str:
        return "echo"

    @property
    def is_connected(self) -> bool:
        return self._connected


class EchoDeliveryProvider:
    @property
    def provider_name(self) -> str:
        return "echo"

    def supports_channel(self, channel_type: str) -> bool:
        return channel_type == "echo"

    async def deliver(self, task: DeliveryTask) -> DeliveryResult:
        return DeliveryResult(
            success=True,
            provider_message_id=f"{task.channel_type}:{task.recipient}",
        )


async def _channel_adapter_contract_round_trips_message():
    adapter = EchoChannel(ChannelConfig(channel_type="echo", allowed_senders=["user"]))

    await adapter.connect()
    receipt = await adapter.send_message("user", "hello")

    assert adapter.is_connected is True
    assert receipt.status is DeliveryStatus.SUCCESS
    assert adapter.config is not None
    assert adapter.config.is_sender_allowed("user")
    assert not adapter.config.is_sender_allowed("stranger")

    msg = ChannelMessage(
        channel_type="echo",
        direction=MessageDirection.INBOUND,
        sender="user",
        recipient="agent",
        content="hi",
        metadata={"thread": "abc"},
    )
    restored = ChannelMessage.from_dict(msg.to_dict())

    assert restored.direction is MessageDirection.INBOUND
    assert restored.metadata == {"thread": "abc"}


async def _delivery_provider_protocol_and_models():
    provider = EchoDeliveryProvider()
    assert isinstance(provider, DeliveryProvider)
    assert provider.supports_channel("echo")

    task = DeliveryTask(
        channel_type="echo",
        recipient="user",
        content={"text": "hello"},
        agent_id="did:kestrel:test",
    )
    result = await provider.deliver(task)

    assert result.success is True
    assert result.to_dict()["provider_message_id"] == "echo:user"
    assert task.to_dict()["agent_id"] == "did:kestrel:test"


def test_async_channel_contract():
    asyncio.run(_channel_adapter_contract_round_trips_message())


def test_async_delivery_contract():
    asyncio.run(_delivery_provider_protocol_and_models())


def test_output_event_envelope_serializes_destination():
    event = OutputEvent(
        kind=OutputKind.CHANNEL_MESSAGE,
        payload={"text": "hello"},
        destination=OutputDestination(channel_type="echo", recipient="user"),
    )

    data = event.to_dict()
    assert data["kind"] == "channel_message"
    assert data["destination"]["channel_type"] == "echo"


def test_entry_point_group_constants_are_stable():
    assert CHANNEL_ADAPTER_ENTRY_POINT_GROUP == "kestrel_sovereign.channel_adapters"
    assert DELIVERY_PROVIDER_ENTRY_POINT_GROUP == "kestrel_sovereign.delivery_providers"
