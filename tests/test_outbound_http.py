"""SSRF guard (#1727): block server-side fetches to non-public addresses."""
from __future__ import annotations

import pytest

from kestrel_sdk.security.outbound import (
    SSRFError,
    ValidatedOutboundURL,
    validate_outbound_url,
    _address_is_blocked,
    _PinnedAsyncNetworkBackend,
)
import ipaddress


@pytest.mark.parametrize("family", ["httpx", "httpx2"])
@pytest.mark.asyncio
async def test_native_transport_rejects_changed_scheme_before_network(family, monkeypatch):
    import importlib
    from kestrel_sdk.security import outbound

    client_module = importlib.import_module(family)
    validated = validate_outbound_url("https://8.8.8.8:443/mcp")
    factory = getattr(outbound, f"pinned_{family}_async_transport")

    async def unexpected_dial(*args, **kwargs):
        pytest.fail("a changed scheme must be rejected before opening a socket")

    core = "httpcore" if family == "httpx" else "httpcore2"
    monkeypatch.setattr(f"{core}._backends.anyio.AnyIOBackend.connect_tcp", unexpected_dial)
    async with client_module.AsyncClient(transport=factory(validated)) as client:
        with pytest.raises(SSRFError, match="scheme"):
            await client.get("http://8.8.8.8:443/mcp")


class TestAddressClassification:
    @pytest.mark.parametrize("addr", [
        "127.0.0.1", "::1",            # loopback
        "169.254.169.254",            # cloud metadata (link-local)
        "10.0.0.5", "192.168.1.1", "172.16.0.1",  # RFC1918
        "0.0.0.0",                    # unspecified
        "fd00::1",                    # IPv6 ULA (private)
        "::ffff:169.254.169.254",     # IPv4-mapped metadata
        "100.64.1.1",                 # shared-address space (neither private nor global)
        "224.0.0.1",                  # multicast
        "64:ff9b::7f00:1", "::7f00:1",  # reserved transition formats
        "fec0::1",                    # deprecated IPv6 site-local
        "192.88.99.2", "::ffff:192.88.99.2",  # IANA non-global 6a44 relay
    ])
    def test_blocked_addresses(self, addr):
        assert _address_is_blocked(ipaddress.ip_address(addr)) is True

    @pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
    def test_public_addresses_allowed(self, addr):
        assert _address_is_blocked(ipaddress.ip_address(addr)) is False


class TestValidateOutboundUrl:
    @pytest.mark.parametrize("address", ["169.254.169.254", "100.64.1.1", "fec0::1"])
    def test_direct_construction_cannot_bypass_public_address_policy(self, address):
        with pytest.raises(SSRFError, match="non-public"):
            ValidatedOutboundURL(url="https://public.example/mcp", scheme="https", host="public.example", port=443, ip_addresses=(ipaddress.ip_address(address),))

    def test_allowed_tls_websocket_scheme_resolves_the_standard_port(self, monkeypatch):
        calls = []

        def resolver(host, port, **kwargs):
            calls.append((host, port))
            return [(None, None, None, None, ("8.8.8.8", port))]

        monkeypatch.setattr("socket.getaddrinfo", resolver)
        result = validate_outbound_url("wss://socket.example/path", allowed_schemes=("wss",))
        assert result.port == 443
        assert calls == [("socket.example", 443)]
    def test_rejected_literal_never_uses_dns_as_another_path(self, monkeypatch):
        def unexpected_dns(*args, **kwargs):
            pytest.fail("a blocked literal must be rejected before DNS")

        monkeypatch.setattr("socket.getaddrinfo", unexpected_dns)
        with pytest.raises(SSRFError, match="non-public"):
            validate_outbound_url("https://169.254.169.254/mcp")

    def test_explicit_zero_port_is_not_replaced_by_default(self):
        assert validate_outbound_url("https://8.8.8.8:0/mcp").port == 0

    @pytest.mark.parametrize("answer", [[], None])
    def test_unresolvable_host_is_an_explicit_rejection(self, monkeypatch, answer):
        import socket

        def resolver(*args, **kwargs):
            if answer is None:
                raise socket.gaierror("host unavailable")
            return answer

        monkeypatch.setattr(socket, "getaddrinfo", resolver)
        with pytest.raises(SSRFError, match="cannot resolve"):
            validate_outbound_url("https://unresolvable.example/mcp")

    def test_empty_validated_address_set_is_rejected(self):
        with pytest.raises(SSRFError, match="no addresses"):
            ValidatedOutboundURL(url="https://public.example", scheme="https", host="public.example", port=443, ip_addresses=())

    def test_invalid_idna_origin_is_an_explicit_rejection(self):
        validated = validate_outbound_url("https://8.8.8.8/mcp")
        with pytest.raises(SSRFError, match="valid IDNA"):
            validated.assert_origin("\ud800.example", 443)

    def test_literal_metadata_ip_rejected(self):
        with pytest.raises(SSRFError):
            validate_outbound_url("http://169.254.169.254/latest/meta-data/")

    def test_literal_loopback_rejected(self):
        with pytest.raises(SSRFError):
            validate_outbound_url("http://127.0.0.1:8888/api/auth/key")

    def test_literal_private_rejected(self):
        with pytest.raises(SSRFError):
            validate_outbound_url("https://10.1.2.3/internal")

    def test_public_literal_ip_allowed(self):
        validate_outbound_url("https://8.8.8.8/")  # no raise

    def test_disallowed_scheme_rejected(self):
        with pytest.raises(SSRFError):
            validate_outbound_url("file:///etc/passwd")
        with pytest.raises(SSRFError):
            validate_outbound_url("gopher://127.0.0.1/")

    def test_https_only_for_didweb(self):
        with pytest.raises(SSRFError):
            validate_outbound_url("http://example.com/x", allowed_schemes=("https",))

    def test_missing_host_rejected(self):
        with pytest.raises(SSRFError):
            validate_outbound_url("https:///nohost")

    def test_localhost_hostname_rejected(self):
        # Resolves to loopback → blocked.
        with pytest.raises(SSRFError):
            validate_outbound_url("http://localhost:9999/")

    @pytest.mark.parametrize("bad", [
        "http://example.com:abc",       # non-numeric port
        "http://[not-an-ipv6/",         # malformed IPv6 literal
    ])
    def test_malformed_url_raises_ssrferror_not_valueerror(self, bad):
        # #1727 codex r1: malformed input must surface as SSRFError (→ 400),
        # never an uncaught ValueError (→ 500).
        with pytest.raises(SSRFError):
            validate_outbound_url(bad)

    def test_validation_returns_pinned_public_ip(self, monkeypatch):
        def fake_getaddrinfo(host, port, proto=0, type=0):
            assert host == "assets.example"
            assert port == 443
            return [
                (None, None, None, None, ("2001:4860:4860::8888", port)),
                (None, None, None, None, ("93.184.216.34", port)),
            ]

        monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
        validated = validate_outbound_url("https://assets.example/avatar.png")

        assert validated.host == "assets.example"
        assert validated.port == 443
        assert [str(ip) for ip in validated.ip_addresses] == [
            "2001:4860:4860::8888",
            "93.184.216.34",
        ]
        assert str(validated.ip_addresses[0]) == "2001:4860:4860::8888"

    def test_validation_rejects_mixed_public_private_dns_before_connect(
        self,
        monkeypatch,
    ):
        calls = []

        def fake_getaddrinfo(host, port, proto=0, type=0):
            return [
                (None, None, None, None, ("93.184.216.34", port)),
                (None, None, None, None, ("127.0.0.1", port)),
            ]

        async def fake_connect_tcp(self, host, port, **kwargs):
            calls.append((host, port))
            return object()

        monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
        monkeypatch.setattr(
            "httpcore._backends.anyio.AnyIOBackend.connect_tcp",
            fake_connect_tcp,
        )

        with pytest.raises(SSRFError):
            validate_outbound_url("https://mixed.example/avatar.png")

        assert calls == []


class TestPinnedConnections:
    @pytest.mark.asyncio
    async def test_httpx_backend_dials_pinned_ip_not_hostname(self, monkeypatch):
        calls = []

        async def fake_connect_tcp(
            self,
            host,
            port,
            timeout=None,
            local_address=None,
            socket_options=None,
        ):
            calls.append((host, port))
            return object()

        monkeypatch.setattr(
            "httpcore._backends.anyio.AnyIOBackend.connect_tcp",
            fake_connect_tcp,
        )
        validated = validate_outbound_url("https://93.184.216.34/avatar.png")
        from httpcore._backends.anyio import AnyIOBackend
        backend = _PinnedAsyncNetworkBackend(validated, backend=AnyIOBackend(), connect_errors=(OSError,))

        await backend.connect_tcp(validated.host, validated.port)

        assert calls == [("93.184.216.34", 443)]

    @pytest.mark.asyncio
    async def test_httpx_backend_accepts_punycode_connect_host_for_validated_idn(
        self,
        monkeypatch,
    ):
        calls = []

        def fake_getaddrinfo(host, port, proto=0, type=0):
            assert host == "bücher.example"
            return [
                (None, None, None, None, ("93.184.216.34", port)),
            ]

        async def fake_connect_tcp(
            self,
            host,
            port,
            timeout=None,
            local_address=None,
            socket_options=None,
        ):
            calls.append((host, port))
            return object()

        monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
        monkeypatch.setattr(
            "httpcore._backends.anyio.AnyIOBackend.connect_tcp",
            fake_connect_tcp,
        )
        validated = validate_outbound_url("https://bücher.example/avatar.png")
        from httpcore._backends.anyio import AnyIOBackend
        backend = _PinnedAsyncNetworkBackend(validated, backend=AnyIOBackend(), connect_errors=(OSError,))

        await backend.connect_tcp("xn--bcher-kva.example", validated.port)

        assert validated.host == "bücher.example"
        assert str(validated.ip_addresses[0]) == "93.184.216.34"
        assert calls == [("93.184.216.34", 443)]

    @pytest.mark.asyncio
    async def test_httpx_backend_falls_back_to_second_validated_ip(
        self,
        monkeypatch,
    ):
        calls = []

        def fake_getaddrinfo(host, port, proto=0, type=0):
            assert host == "dualstack.example"
            return [
                (None, None, None, None, ("2001:4860:4860::8888", port)),
                (None, None, None, None, ("93.184.216.34", port)),
            ]

        async def fake_connect_tcp(
            self,
            host,
            port,
            timeout=None,
            local_address=None,
            socket_options=None,
        ):
            calls.append((host, port))
            if host == "2001:4860:4860::8888":
                raise OSError("IPv6 route unavailable")
            return object()

        monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
        monkeypatch.setattr(
            "httpcore._backends.anyio.AnyIOBackend.connect_tcp",
            fake_connect_tcp,
        )
        validated = validate_outbound_url("https://dualstack.example/avatar.png")
        from httpcore._backends.anyio import AnyIOBackend
        backend = _PinnedAsyncNetworkBackend(validated, backend=AnyIOBackend(), connect_errors=(OSError,))

        await backend.connect_tcp(validated.host, validated.port)

        assert calls == [
            ("2001:4860:4860::8888", 443),
            ("93.184.216.34", 443),
        ]
