"""DNS approval and every HTTP client must identify the same IDNA origin."""

import importlib
import ipaddress
import socket

import pytest

from kestrel_sdk.security.outbound import SSRFError, ValidatedOutboundURL, validate_outbound_url


@pytest.mark.parametrize("hostname,canonical", [
    ("straße.example", "xn--strae-oqa.example"),
    ("ς.example", "xn--3xa.example"),
    ("bücher.example", "xn--bcher-kva.example"),
])
@pytest.mark.parametrize("family", ["httpx", "httpx2"])
def test_dns_url_and_native_wire_share_the_idna2008_origin(monkeypatch, hostname, canonical, family):
    def resolve(host, port, *, type, proto):
        assert host == canonical
        assert type == socket.SOCK_STREAM and proto == socket.IPPROTO_TCP
        return [(socket.AF_INET, type, proto, "", ("8.8.8.8", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    original = f"https://{hostname}:8443/avatar.png?size=1#preview"
    validated = validate_outbound_url(original)
    native = importlib.import_module(family).URL(original)
    assert validated.host == canonical
    assert validated.url == f"https://{canonical}:8443/avatar.png?size=1#preview"
    validated.assert_origin(native.host, 8443)
    validated.assert_origin(native.raw_host.decode("ascii"), 8443)


def test_idna2003_lookalike_is_a_different_approved_origin(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port)),
    ])
    validated = validate_outbound_url("https://straße.example/avatar.png")
    with pytest.raises(SSRFError, match="unexpected origin"):
        validated.assert_origin("strasse.example", 443)


@pytest.mark.parametrize("url", [
    "https://user@public.example/avatar.png",
    "https://user:password@public.example/avatar.png",
    "https://@public.example/avatar.png",
    "https://a\u200db.example/avatar.png",
])
def test_unusable_authorities_are_rejected_before_dns(monkeypatch, url):
    def unexpected_dns(*args, **kwargs):
        pytest.fail("invalid authority reached DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    with pytest.raises(SSRFError):
        validate_outbound_url(url)


def test_direct_value_cannot_disagree_with_its_url_origin():
    with pytest.raises(SSRFError, match="origin"):
        ValidatedOutboundURL(
            url="https://straße.example/avatar.png", scheme="https", host="strasse.example", port=443,
            ip_addresses=(ipaddress.ip_address("8.8.8.8"),),
        )


def test_direct_value_normalizes_unicode_aliases_for_native_io():
    validated = ValidatedOutboundURL(
        url="https://straße.example/avatar.png", scheme="https", host="straße.example", port=443,
        ip_addresses=(ipaddress.ip_address("8.8.8.8"),),
    )
    assert validated.host == "xn--strae-oqa.example"
    assert validated.url == "https://xn--strae-oqa.example/avatar.png"


@pytest.mark.parametrize("url", [
    pytest.param("https://public.example:" + "9" * 10000, id="oversized-port"),
    pytest.param("https:///" + "a" * 10000, id="missing-host"),
    pytest.param("a" * 10000 + "://public.example", id="unsupported-scheme"),
])
def test_validation_diagnostics_do_not_echo_unbounded_url_input(url):
    with pytest.raises(SSRFError) as error:
        validate_outbound_url(url)
    assert len(str(error.value)) <= 512


@pytest.mark.parametrize("suffix", ["/p%2Fq;params?x=%25#", "/a?", "/a?#fragment", "/a#"])
def test_canonical_authority_preserves_the_original_request_suffix(monkeypatch, suffix):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port)),
    ])
    validated = validate_outbound_url("https://straße.example" + suffix)
    assert validated.url == "https://xn--strae-oqa.example" + suffix


@pytest.mark.parametrize("url", [" https://public.example/x", "https://public.example/x\n", "https://pub\tlic.example/x"])
def test_raw_url_control_characters_are_not_silently_discarded(monkeypatch, url):
    def unexpected_dns(*args, **kwargs):
        pytest.fail("malformed raw URL reached DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    with pytest.raises(SSRFError):
        validate_outbound_url(url)
