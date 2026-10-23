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
from jwt.exceptions import PyJWKClientConnectionError, PyJWKError


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


@pytest.mark.parametrize(
    'failure',
    [
        ValueError('verification key store unavailable'),
        PyJWKClientConnectionError('verification key endpoint unavailable'),
    ],
)
def test_verifier_does_not_swallow_key_provider_failures(
    failure: Exception,
) -> None:

    def provider(kid: str | None, jku: str | None) -> bytes:
        raise failure

    verify = signing.create_signature_verifier(provider, ['ES256'])
    with pytest.raises(type(failure)) as caught:
        verify(_card(_ACCEPT[0]['served_card']))
    assert caught.value is failure


@pytest.mark.parametrize('hostile_algorithm', ['HS256', 'RS256'])
@pytest.mark.parametrize('valid_follows', [False, True])
def test_hostile_key_family_does_not_hide_independent_ec_signature(
    hostile_algorithm: str,
    valid_follows: bool,
) -> None:
    card = _card(_ACCEPT[0]['served_card'])
    good = list(card.signatures)
    del card.signatures[:]
    header = json.dumps(
        {'alg': hostile_algorithm, 'kid': _VECTORS['test_key']['jwk']['kid']}
    ).encode()
    card.signatures.append(
        AgentCardSignature(
            protected=base64.urlsafe_b64encode(header).rstrip(b'=').decode(),
            signature='AAAA',
        )
    )
    if valid_follows:
        card.signatures.extend(good)
    key = PyJWK(_VECTORS['test_key']['jwk']).key
    verify = signing.create_signature_verifier(
        lambda kid, jku: key, ['ES256', hostile_algorithm]
    )
    if valid_follows:
        verify(card)
    else:
        with pytest.raises(signing.InvalidSignaturesError):
            verify(card)


@pytest.mark.parametrize('valid_follows', [False, True])
def test_hostile_asymmetric_header_does_not_hide_valid_secret_signature(
    valid_follows: bool,
) -> None:
    card = _card(_ACCEPT[0]['served_card'])
    del card.signatures[:]
    key = b'a-real-test-secret-of-at-least-forty-eight-bytes-for-hs384'
    signed = signing.create_agent_card_signer(
        key,
        {
            'alg': 'HS384',
            'kid': 'key1',
            'typ': 'JOSE',
            'jku': None,
        },
    )(card)
    good = list(signed.signatures)
    del signed.signatures[:]
    signed.signatures.append(
        AgentCardSignature(
            protected=base64.urlsafe_b64encode(b'{"alg":"ES256","kid":"key1"}')
            .rstrip(b'=')
            .decode(),
            signature='AAAA',
        )
    )
    if valid_follows:
        signed.signatures.extend(good)
    verify = signing.create_signature_verifier(
        lambda kid, jku: key, ['HS256', 'HS384', 'ES256', 'RS256']
    )
    if valid_follows:
        verify(signed)
    else:
        with pytest.raises(signing.InvalidSignaturesError):
            verify(signed)


def test_provider_explicitly_rejects_one_key_then_verifies_independent_signature() -> (
    None
):
    card = _card(_ACCEPT[0]['served_card'])
    good = list(card.signatures)
    del card.signatures[:]
    card.signatures.append(
        AgentCardSignature(
            protected=base64.urlsafe_b64encode(
                b'{"alg":"ES256","kid":"untrusted"}'
            )
            .rstrip(b'=')
            .decode(),
            signature='AAAA',
        )
    )
    card.signatures.extend(good)

    def provider(kid: str | None, jku: str | None) -> PyJWK:
        if kid == 'untrusted':
            raise PyJWKError('untrusted signing key')
        return PyJWK(_VECTORS['test_key']['jwk'])

    signing.create_signature_verifier(provider, ['ES256'])(card)
