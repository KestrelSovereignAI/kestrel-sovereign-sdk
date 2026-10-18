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


@pytest.mark.parametrize("url,host,port", [
    ("https://straße.example/avatar.png", "strasse.example", 443),
    ("https://public.example:8443/x", "public.example", 443),
    ("https://public.example:0/x", "public.example", 443),
])
def test_direct_value_cannot_disagree_with_its_url_origin(url, host, port):
    with pytest.raises(SSRFError, match="origin"):
        ValidatedOutboundURL(
            url=url, scheme="https", host=host, port=port,
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


@pytest.mark.parametrize("host", ["my_host.example", "_svc.example", "ab--cd.example", "-lead.example", "trail-.example", "xn--zzzz.example"])
def test_dns_name_policy_rejects_non_idna_ascii_names_before_resolution(monkeypatch, host):
    def unexpected_dns(*args, **kwargs):
        pytest.fail("non-IDNA hostname reached DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    with pytest.raises(SSRFError, match="IDNA"):
        validate_outbound_url(f"https://{host}/avatar.png")


@pytest.mark.parametrize("family", ["httpx", "httpx2"])
@pytest.mark.parametrize("authority,expected,port", [
    ("[2001:4860:4860:0000:0000:0000:0000:8888]", "[2001:4860:4860::8888]", 443),
    ("[2001:4860:4860::8888]:8443", "[2001:4860:4860::8888]:8443", 8443),
])
def test_public_ipv6_literal_preserves_a_native_parseable_authority(monkeypatch, family, authority, expected, port):
    def unexpected_dns(*args, **kwargs):
        pytest.fail("literal IP must not resolve a hostname")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    validated = validate_outbound_url(f"https://{authority}/p?x#")
    assert validated.url == f"https://{expected}/p?x#"
    assert validated.host == "2001:4860:4860::8888"
    assert validated.port == port
    assert validated.ip_addresses == (ipaddress.ip_address("2001:4860:4860::8888"),)
    native = importlib.import_module(family).URL(validated.url)
    validated.assert_origin(native.raw_host.decode("ascii"), native.port or 443)


def test_direct_record_unknown_scheme_diagnostic_is_bounded():
    scheme = "a" * 10000
    with pytest.raises(SSRFError) as error:
        ValidatedOutboundURL(
            url=scheme + "://public.example/path", scheme=scheme, host="public.example", port=443,
            ip_addresses=(ipaddress.ip_address("8.8.8.8"),),
        )
    assert len(str(error.value)) <= 512


@pytest.mark.parametrize("host", ["01.2.3.4", "0177.0.0.1", "1.2.3.04", "1.02.3.4", "1.2.3.999", "1。2。3。04"])
def test_invalid_ipv4_shaped_authorities_are_refused_before_dns(monkeypatch, host):
    def unexpected_dns(*args, **kwargs):
        pytest.fail("invalid IPv4-shaped origin reached DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    with pytest.raises(SSRFError, match="IP literal"):
        validate_outbound_url(f"https://{host}/avatar.png")


@pytest.mark.parametrize("family", ["httpx", "httpx2"])
@pytest.mark.parametrize("host", ["8。8。8。8", "8．8．8．8"])
def test_idna_separator_mapping_to_a_valid_ipv4_still_round_trips(monkeypatch, family, host):
    def unexpected_dns(*args, **kwargs):
        pytest.fail("a canonical IP literal must not query DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    validated = validate_outbound_url(f"https://{host}/avatar.png")
    assert validated.url == "https://8.8.8.8/avatar.png"
    assert validated.host == "8.8.8.8"
    assert validated.ip_addresses == (ipaddress.ip_address("8.8.8.8"),)
    assert importlib.import_module(family).URL(validated.url).raw_host == b"8.8.8.8"


@pytest.mark.parametrize("host", ["numeric123.example", "1.2.3.4.5", "2130706433", "01.2.3.4."])
def test_other_numeric_dns_names_remain_subject_to_normal_resolution(monkeypatch, host):
    seen = []
    def resolve(name, port, **kwargs):
        seen.append(name)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    validated = validate_outbound_url(f"https://{host}/avatar.png")
    assert seen == [host]
    assert validated.host == host
    assert validated.ip_addresses == (ipaddress.ip_address("8.8.8.8"),)
    for family in ("httpx", "httpx2"):
        assert importlib.import_module(family).URL(validated.url).raw_host.decode("ascii") == host


@pytest.mark.parametrize("url,candidate", [
    ("https://pub.example/x", "PUB.Example"),
    ("https://straße.example/x", "STRAẞE.EXAMPLE"),
    ("https://[2001:4860:4860::8888]/x", "[2001:4860:4860::8888]"),
])
def test_public_origin_check_accepts_case_and_bracket_equivalents(monkeypatch, url, candidate):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port)),
    ])
    validate_outbound_url(url).assert_origin(candidate, 443)


@pytest.mark.parametrize("host,addresses", [
    ("8.8.8.8", ("1.1.1.1",)),
    ("8.8.8.8", ("8.8.8.8", "1.1.1.1")),
    ("2001:4860:4860::8888", ("2606:4700:4700::1111",)),
])
def test_direct_ip_literal_records_cannot_approve_a_different_destination(host, addresses):
    authority = f"[{host}]" if ":" in host else host
    with pytest.raises(SSRFError, match="literal"):
        ValidatedOutboundURL(url=f"https://{authority}/x", scheme="https", host=host, port=443,
                             ip_addresses=tuple(ipaddress.ip_address(address) for address in addresses))


@pytest.mark.parametrize("kind", ["authority", "idna", "address"])
def test_untrusted_validation_values_do_not_escape_through_logged_tracebacks(kind):
    import traceback
    marker = "untrusted-input-marker" * 1000
    with pytest.raises(SSRFError) as error:
        if kind == "authority":
            validate_outbound_url("https://" + marker + "／example.com/x")
        elif kind == "idna":
            validate_outbound_url("https://" + marker + "!.example/x")
        else:
            ValidatedOutboundURL(url="https://public.example/x", scheme="https", host="public.example", port=443,
                                 ip_addresses=(marker,))
    rendered = "".join(traceback.format_exception(error.value))
    assert len(rendered) < 4096
    assert marker not in rendered


@pytest.mark.parametrize("suffix", ["/a\ud800", "/p?q=\udfff", "/p#\ud800"])
def test_unencodable_request_suffix_is_refused_before_dns(monkeypatch, suffix):
    def unexpected_dns(*args, **kwargs):
        pytest.fail("unencodable URL reached DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    with pytest.raises(SSRFError, match="UTF-8"):
        validate_outbound_url("https://public.example" + suffix)


@pytest.mark.parametrize("url,host,port", [("https://[v1.example.com]/x", "v1.example.com", 443), ("https://[vF.public.example]:8443/x", "vf.public.example", 8443)])
def test_unsupported_ip_literal_authorities_never_become_dns_names(monkeypatch, url, host, port):
    def unexpected_dns(*args, **kwargs):
        pytest.fail("unsupported bracketed literal was reinterpreted as DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    with pytest.raises(SSRFError, match="literal"):
        validate_outbound_url(url)
    with pytest.raises(SSRFError, match="literal"):
        ValidatedOutboundURL(url=url, scheme="https", host=host, port=port,
                             ip_addresses=(ipaddress.ip_address("8.8.8.8"),))


@pytest.mark.parametrize("candidate", ["[v1.example.com]", "[8.8.8.8]"])
def test_bracketed_origin_candidates_require_an_ipv6_literal(candidate):
    validated = validate_outbound_url("https://8.8.8.8/x")
    with pytest.raises(SSRFError, match="literal"):
        validated.assert_origin(candidate, 443)


@pytest.mark.parametrize("address", ["2001:4860:4860::8888%en0", "2001:4860:4860::8888%é", "::ffff:8.8.8.8%en0"])
def test_public_fetch_policy_refuses_local_ipv6_scope_hints(monkeypatch, address):
    def unexpected_dns(*args, **kwargs):
        pytest.fail("scoped IP must not resolve DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    with pytest.raises(SSRFError, match="non-public"):
        validate_outbound_url(f"https://[{address}]/x")
    with pytest.raises(SSRFError, match="non-public"):
        ValidatedOutboundURL(url="https://public.example/x", scheme="https", host="public.example", port=443,
                             ip_addresses=(ipaddress.ip_address(address),))


@pytest.mark.parametrize("authority", [
    "trusted.example[attacker.example]", "trusted.example[8.8.4.4]",
    "[2001:4860:4860::8888]trusted.example", "junk[2001:4860:4860::8888]:8443",
    "[2001:4860:4860::8888]trusted.example:8443",
])
@pytest.mark.parametrize("permissive_parser", [False, True])
def test_parser_components_do_not_authorize_misplaced_brackets(monkeypatch, authority, permissive_parser):
    from urllib.parse import ParseResult
    from kestrel_sdk.security import outbound

    url = f"https://{authority}/report"
    if permissive_parser:
        # A parser supplies components, not a validity guarantee. These public
        # ParseResult values reproduce actual CPython 3.11.1 extraction without
        # depending on private stdlib validators or the runner's patch version.
        parsed = ParseResult("https", authority, "/report", "", "", "")
        original_parser = outbound.urlparse
        monkeypatch.setattr(outbound, "urlparse", lambda candidate: parsed if candidate == url else original_parser(candidate))

    def unexpected_dns(*args, **kwargs):
        pytest.fail("misplaced brackets were rewritten into a DNS authority")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    with pytest.raises(SSRFError, match="authority"):
        validate_outbound_url(url)
