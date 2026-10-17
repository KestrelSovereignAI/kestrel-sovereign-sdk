"""Native client I/O keeps approved IPs, TLS identity and the HTTP origin bound."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import importlib
import ipaddress
from pathlib import Path
import socket
import ssl
import subprocess
import sys
from unittest.mock import AsyncMock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

from kestrel_sdk.security import outbound


@pytest.fixture(params=["httpx", "httpx2"])
def native_client(request):
    family = request.param
    module = importlib.import_module(family)
    core = importlib.import_module("httpcore" if family == "httpx" else "httpcore2")
    backend = importlib.import_module(f"{core.__name__}._backends.anyio")
    return module, backend.AnyIOBackend, getattr(outbound, f"pinned_{family}_async_transport")


def _tls_context(tmp_path, hostname, monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / "certificate.pem"
    key_path = tmp_path / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate_path))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    return context


@asynccontextmanager
async def _https_endpoint(context):
    requests, server_names = [], []
    context.set_servername_callback(lambda _socket, name, _context: server_names.append(name))

    async def handle(reader, writer):
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context) as server:
        yield server.sockets[0].getsockname()[1], requests, server_names


@pytest.mark.asyncio
@pytest.mark.parametrize("certificate_host", ["pinned.example.test", "wrong.example.test"])
async def test_native_tls_keeps_host_sni_verification_and_never_resolves_again(
    native_client, certificate_host, tmp_path, monkeypatch,
):
    client_module, backend_type, factory = native_client
    context = _tls_context(tmp_path, certificate_host, monkeypatch)
    async with _https_endpoint(context) as (port, requests, server_names):
        # Validate a public answer, then route only the approved numeric dial to
        # our local TLS fixture. The native socket/TLS/HTTP exchange remains real.
        def initial_dns(host, resolved_port, proto):
            assert host == "pinned.example.test"
            return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", ("8.8.8.8", resolved_port))]

        monkeypatch.setattr(socket, "getaddrinfo", initial_dns)
        validated = outbound.validate_outbound_url(f"https://pinned.example.test:{port}/mcp")
        dials = []
        original_dial = backend_type.connect_tcp

        async def route_approved_dial(self, host, port, **kwargs):
            dials.append((host, port))
            assert host == "8.8.8.8"
            return await original_dial(self, "127.0.0.1", port, **kwargs)

        def reject_dns(*args, **kwargs):
            pytest.fail("a pinned request must not repeat DNS resolution")

        monkeypatch.setattr(socket, "getaddrinfo", reject_dns)
        monkeypatch.setattr(backend_type, "connect_tcp", route_approved_dial)
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "")
        async with client_module.AsyncClient(transport=factory(validated)) as client:
            if certificate_host == validated.host:
                response = await client.get(validated.url)
                assert response.text == "ok"
                assert len(requests) == 1
                assert f"Host: pinned.example.test:{port}\r\n".encode() in requests[0]
            else:
                with pytest.raises(client_module.ConnectError, match="certificate verify failed"):
                    await client.get(validated.url)
                assert requests == []
        assert dials == [("8.8.8.8", port)]
        assert server_names == ["pinned.example.test"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://1.1.1.1/mcp", "https://8.8.8.8:444/mcp"])
async def test_native_transport_rejects_other_origin_before_connect(native_client, url, monkeypatch):
    client_module, backend_type, factory = native_client
    dial = AsyncMock(side_effect=AssertionError("unexpected network dial"))
    monkeypatch.setattr(backend_type, "connect_tcp", dial)
    validated = outbound.validate_outbound_url("https://8.8.8.8/mcp")
    async with client_module.AsyncClient(transport=factory(validated)) as client:
        with pytest.raises(outbound.SSRFError, match="unexpected origin"):
            await client.get(url)
    dial.assert_not_awaited()


@pytest.mark.asyncio
async def test_backend_propagates_unexpected_errors_and_refuses_unix_sockets():
    validated = outbound.ValidatedOutboundURL(
        url="https://public.example/mcp", scheme="https", host="public.example", port=443,
        ip_addresses=(ipaddress.ip_address("8.8.8.8"), ipaddress.ip_address("1.1.1.1")),
    )
    backend = AsyncMock()
    backend.connect_tcp.side_effect = [TypeError("programming error"), object()]
    pinned = outbound._PinnedAsyncNetworkBackend(validated, backend=backend, connect_errors=(OSError,))
    with pytest.raises(TypeError, match="programming error"):
        await pinned.connect_tcp(validated.host, validated.port)
    backend.connect_tcp.assert_awaited_once()
    with pytest.raises(outbound.SSRFError, match="Unix-socket"):
        await pinned.connect_unix_socket("/var/run/docker.sock")
    backend.connect_unix_socket.assert_not_awaited()


@pytest.mark.asyncio
async def test_backend_reports_all_approved_addresses_exhausted():
    validated = outbound.ValidatedOutboundURL(
        url="https://public.example/mcp", scheme="https", host="public.example", port=443,
        ip_addresses=(ipaddress.ip_address("8.8.8.8"), ipaddress.ip_address("1.1.1.1")),
    )
    backend = AsyncMock()
    backend.connect_tcp.side_effect = [OSError("first unreachable"), OSError("second unreachable")]
    pinned = outbound._PinnedAsyncNetworkBackend(validated, backend=backend, connect_errors=(OSError,))
    with pytest.raises(OSError, match="second unreachable"):
        await pinned.connect_tcp(validated.host, validated.port)
    assert [call.kwargs["host"] for call in backend.connect_tcp.await_args_list] == ["8.8.8.8", "1.1.1.1"]


def test_policy_import_does_not_import_optional_http_or_crypto_dependencies():
    script = """
import importlib.abc
import sys
class RejectOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'httpx', 'httpx2', 'httpcore', 'httpcore2', 'anyio', 'cryptography'}:
            raise AssertionError(f'optional dependency imported: {fullname}')
sys.meta_path.insert(0, RejectOptional())
from kestrel_sdk.security.outbound import validate_outbound_url
assert validate_outbound_url('https://8.8.8.8/mcp').host == '8.8.8.8'
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        cwd=Path(__file__).parents[1], timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
