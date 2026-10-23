# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.
"""Cross-SDK Agent Card signature tests.

`signing_interop_vectors.json` holds one card signed four ways: by this SDK,
by @a2a-js/sdk, by a2a-go and by an independent reference signer. The card
carries every REQUIRED field and no field at its default value, so the
canonical readings in use today agree on its bytes, and those expectations
hold however a2aproject/A2A#2122 is settled: a card another SDK signed still
verifies here.
"""

import base64
import json

from pathlib import Path

import pytest

from kestrel_sdk.native_a2a.types import AgentCard, AgentCardSignature
from kestrel_sdk.native_a2a.utils import signing
from google.protobuf import json_format
from jwt import PyJWK


_HERE = Path(__file__).parent
_VECTORS = json.loads(
    (_HERE / 'signing_interop_vectors.json').read_text(encoding='utf-8')
)
_ACCEPT = [v for v in _VECTORS['vectors'] if v['disposition'] == 'MUST-ACCEPT']
_REJECT = [v for v in _VECTORS['vectors'] if v['disposition'] == 'MUST-REJECT']


def _key_provider(jwks: dict):
    def provide(kid: str | None, jku: str | None) -> PyJWK:
        for key in jwks['keys']:
            if key['kid'] == kid:
                return PyJWK(key)
        raise ValueError(f'kid not in key set: {kid}')

    return provide


def _card(served: dict) -> AgentCard:
    return json_format.ParseDict(served, AgentCard())


_VERIFY = signing.create_signature_verifier(
    _key_provider({'keys': [_VECTORS['test_key']['jwk']]}), ['ES256']
)


def test_vector_corpus_is_complete():
    """The corpus must be whole; a partially loaded corpus passes vacuously."""
    assert len(_ACCEPT) == _VECTORS['counts']['accept'] == 4
    assert len(_REJECT) == _VECTORS['counts']['reject'] == 1
    assert {v['signer'] for v in _ACCEPT} >= {
        'a2a-sdk 1.2.1',
        '@a2a-js/sdk 1.3.0',
        'a2a-go main 534a60fc',
    }


@pytest.mark.parametrize('vector', _ACCEPT, ids=lambda v: v['id'])
def test_card_signed_by_any_sdk_verifies(vector):
    """A card signed by any SDK verifies here."""
    _VERIFY(_card(vector['served_card']))


@pytest.mark.parametrize('vector', _ACCEPT, ids=lambda v: v['id'])
def test_card_canonical_bytes_are_the_shared_form(vector):
    """The bytes every SDK and every reading agree on, byte for byte."""
    canonical = signing._canonicalize_agent_card(_card(vector['served_card']))
    assert canonical.encode('utf-8').hex() == vector['canonical_utf8_hex']


@pytest.mark.parametrize('vector', _REJECT, ids=lambda v: v['id'])
def test_card_edited_after_signing_is_rejected(vector):
    """A signature over other bytes must not verify."""
    with pytest.raises(signing.InvalidSignaturesError):
        _VERIFY(_card(vector['served_card']))


_BAD_PROTECTED_HEADERS = [
    base64.urlsafe_b64encode(value).rstrip(b'=').decode()
    for value in [
        b'[]',
        b'"x"',
        b'null',
        b'nope',
        b'\xff\xfe',
        b'{"kid":[]}',
        b'{"jku":{}}',
        b'[' * 2000 + b']' * 2000,
    ]
] + ['a']
_BAD_HEADER_IDS = [
    'array',
    'string',
    'null',
    'json',
    'utf8',
    'kid',
    'jku',
    'depth',
    'base64',
]


@pytest.mark.parametrize('vector', _ACCEPT, ids=lambda v: v['id'])
@pytest.mark.parametrize(
    'protected', _BAD_PROTECTED_HEADERS, ids=_BAD_HEADER_IDS
)
def test_malformed_signature_does_not_hide_independent_valid_signature(
    vector: dict, protected: str
) -> None:
    """A malformed first signature cannot invalidate a later JS/Go/reference signature."""
    card = _card(vector['served_card'])
    good = list(card.signatures)
    del card.signatures[:]
    card.signatures.append(
        AgentCardSignature(protected=protected, signature='AAAA')
    )
    card.signatures.extend(good)
    _VERIFY(card)


@pytest.mark.parametrize(
    'protected', _BAD_PROTECTED_HEADERS, ids=_BAD_HEADER_IDS
)
def test_malformed_only_signature_has_a_verification_error(
    protected: str,
) -> None:
    card = _card(_ACCEPT[0]['served_card'])
    del card.signatures[:]
    card.signatures.append(
        AgentCardSignature(protected=protected, signature='AAAA')
    )
    with pytest.raises(signing.InvalidSignaturesError):
        _VERIFY(card)


def test_verifier_does_not_swallow_key_provider_failures() -> None:
    failure = ValueError('verification key store unavailable')

    def provider(kid: str | None, jku: str | None) -> bytes:
        raise failure

    verify = signing.create_signature_verifier(provider, ['ES256'])
    with pytest.raises(ValueError) as caught:
        verify(_card(_ACCEPT[0]['served_card']))
    assert caught.value is failure
