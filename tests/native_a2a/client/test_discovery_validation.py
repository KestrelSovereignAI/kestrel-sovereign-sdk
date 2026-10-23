# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.
"""Peer-controlled discovery uses the same decoder as native responses."""

import json

from pathlib import Path

import httpx
import pytest

from kestrel_sdk.native_a2a.client import (
    A2ACardResolver,
    AgentCardResolutionError,
    ClientConfig,
    ClientFactory,
)
from kestrel_sdk.native_a2a.types import AgentCard


_VECTORS = json.loads(
    (
        Path(__file__).parents[1] / 'utils' / 'signing_interop_vectors.json'
    ).read_text()
)
_VALID_CARD = _VECTORS['vectors'][0]['served_card']


@pytest.mark.parametrize(
    'body',
    [
        b'{"name":"n","\\ud800":1}',
        b'{"name":"\\udfff"}',
        b'{"name":"\xff"}',
        b'{"name":"n","x":' + b'[' * 2000 + b']' * 2000 + b'}',
        b'{"name":"n","version":NaN}',
        b'{"name":"n","version":1e999}',
        b'{"name":"first","name":"second"}',
        json.dumps({**_VALID_CARD, 'capabilities': 'zzz'}).encode(),
        json.dumps({**_VALID_CARD, 'supportedInterfaces': 'zzz'}).encode(),
        json.dumps({**_VALID_CARD, 'skills': [42]}).encode(),
    ],
    ids=[
        'surrogate-key',
        'surrogate-value',
        'utf8',
        'depth',
        'nan',
        'infinity',
        'duplicate-key',
        'capabilities-shape',
        'interfaces-shape',
        'skill-shape',
    ],
)
@pytest.mark.asyncio
async def test_discovery_normalizes_peer_decoding_errors(body: bytes) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(AgentCardResolutionError):
            await ClientFactory(
                ClientConfig(httpx_client=http_client)
            ).create_from_url('https://peer.example')


@pytest.mark.asyncio
async def test_discovery_preserves_forward_fields_without_mutating_the_card() -> (
    None
):
    served = {**_VALID_CARD, 'futureField': {'unicode': '雪', 'value': [1, 2]}}
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=served))
    async with httpx.AsyncClient(transport=transport) as http_client:
        card = await A2ACardResolver(
            http_client, 'https://peer.example'
        ).get_agent_card()
    assert card.name == _VALID_CARD['name']
    assert (
        card.signatures[0].protected
        == _VALID_CARD['signatures'][0]['protected']
    )


@pytest.mark.parametrize(
    'error', [ValueError('key store'), json.JSONDecodeError('key store', '', 0)]
)
@pytest.mark.asyncio
async def test_discovery_does_not_misclassify_verifier_failures(
    error: Exception,
) -> None:
    def verifier(_: AgentCard) -> None:
        raise error

    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json=_VALID_CARD)
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(type(error)) as caught:
            await A2ACardResolver(
                http_client, 'https://peer.example'
            ).get_agent_card(signature_verifier=verifier)
    assert caught.value is error
