"""Shared outbound URL validation and DNS-pinned HTTP transports.

The policy imports only the standard library. Install the matching ``httpx``
or ``httpx2`` SDK extra to construct that client's native transport. Validate
before connecting; a transport is bound to that one origin and its approved
IP set. Redirect destinations require separate validation and a new transport.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib
import ipaddress
import socket
from types import ModuleType
from typing import Any
from urllib.parse import urlparse


class SSRFError(ValueError):
    """Raised when an outbound URL targets a disallowed (non-public) address."""


@dataclass(frozen=True)
class ValidatedOutboundURL:
    """One resolved origin whose approved IPs must be used for connection."""

    url: str
    scheme: str
    host: str
    port: int
    ip_addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]

    def __post_init__(self) -> None:
        addresses = tuple(self.ip_addresses)
        if not addresses:
            raise SSRFError("validated outbound URL has no addresses")
        object.__setattr__(self, "ip_addresses", addresses)

    def assert_origin(self, host: str, port: int) -> None:
        """Refuse a request that escapes the origin whose DNS was validated."""
        if not _origin_hosts_match(host, self.host) or port != self.port:
            raise SSRFError(f"pinned transport refused unexpected origin {host}:{port}")


def _address_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address that must not be reachable from a server-side fetch.

    ``is_private`` already covers RFC1918, loopback, link-local (169.254/16 — the
    cloud-metadata range), unique-local IPv6 (fd00::/8), and more on modern
    Python; we add the remaining non-global categories explicitly for clarity and
    forward-compatibility.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def _resolve_addresses(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve ``host`` to all IPs (blocking). Raises SSRFError on failure."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SSRFError(f"cannot resolve host {host!r}: {e}") from e
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def _canonical_origin_host(host: str) -> str:
    """Return the comparable origin host form used by connection backends."""
    host = host.strip("[]")
    try:
        return host.encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise SSRFError("outbound origin host is not a valid IDNA name") from error


def _origin_hosts_match(candidate: str, validated: str) -> bool:
    return _canonical_origin_host(candidate) == _canonical_origin_host(validated)


def validate_outbound_url(
    url: str, *, allowed_schemes: "tuple[str, ...]" = ("http", "https")
) -> ValidatedOutboundURL:
    """Validate ``url`` for a server-side fetch; raise :class:`SSRFError` if unsafe.

    Synchronous (uses ``socket.getaddrinfo``). Prefer :func:`assert_safe_url` in
    async code so DNS resolution doesn't block the event loop. The returned
    :class:`ValidatedOutboundURL` contains only public IPs that passed validation;
    fetchers must connect to those IPs instead of resolving the hostname again.
    """
    # Malformed URLs (bad port, invalid IPv6 literal, …) make urlparse/.port
    # raise ValueError; convert to SSRFError so callers get a clean rejection
    # (400) instead of an uncaught 500 (codex r1).
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
    except ValueError as e:
        raise SSRFError(f"malformed URL {url!r}: {e}") from e

    if scheme not in allowed_schemes:
        raise SSRFError(
            f"URL scheme {parsed.scheme!r} not allowed (allowed: {allowed_schemes})"
        )
    if not host:
        raise SSRFError(f"URL has no host: {url!r}")

    # A rejected literal is not a hostname-resolution failure. Do not catch
    # our own SSRFError (a ValueError) and repeat DNS for a blocked address.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _address_is_blocked(ip):
            raise SSRFError(f"URL host {host} is a non-public address")
        return ValidatedOutboundURL(
            url=url, scheme=scheme, host=host, port=port, ip_addresses=(ip,)
        )

    resolved = _resolve_addresses(host, port)
    if not resolved:
        raise SSRFError(f"cannot resolve host {host!r}: no addresses returned")
    for ip in resolved:
        if _address_is_blocked(ip):
            raise SSRFError(
                f"URL host {host!r} resolves to a non-public address ({ip})"
            )
    return ValidatedOutboundURL(
        url=url, scheme=scheme, host=host, port=port, ip_addresses=tuple(resolved)
    )


async def assert_safe_url(
    url: str, *, allowed_schemes: "tuple[str, ...]" = ("http", "https")
) -> ValidatedOutboundURL:
    """Async wrapper: validate ``url`` off the event loop (DNS in a thread)."""
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: validate_outbound_url(url, allowed_schemes=allowed_schemes)
    )


class _PinnedAsyncNetworkBackend:
    """One origin check and dial policy for either native HTTP client family."""

    def __init__(self, validated: ValidatedOutboundURL, *, backend: Any,
                 connect_errors: tuple[type[Exception], ...]):
        self._validated = validated
        self._backend = backend
        self._connect_errors = connect_errors

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None, socket_options: Any = None):
        self._validated.assert_origin(host, port)
        last_error = None
        for address in self._validated.ip_addresses:
            try:
                return await self._backend.connect_tcp(
                    host=str(address), port=port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except self._connect_errors as error:
                last_error = error
        if last_error is not None:
            raise last_error
        raise SSRFError("pinned transport has no validated addresses")

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Any = None):
        raise SSRFError("pinned transport does not allow Unix-socket connections")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def _pinned_async_transport(validated: ValidatedOutboundURL, *,
                            httpx_module: ModuleType, httpcore_module: ModuleType):
    config = importlib.import_module(f"{httpx_module.__name__}._config")
    backends = importlib.import_module(f"{httpcore_module.__name__}._backends.anyio")
    backend = _PinnedAsyncNetworkBackend(
        validated, backend=backends.AnyIOBackend(),
        connect_errors=(httpcore_module.NetworkError, httpcore_module.TimeoutException, OSError),
    )

    class PinnedAsyncHTTPTransport(httpx_module.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            if request.url.scheme != validated.scheme:
                raise SSRFError("pinned transport refused a changed URL scheme")
            port = request.url.port
            validated.assert_origin(
                request.url.host,
                port if port is not None else (443 if request.url.scheme == "https" else 80),
            )
            return await super().handle_async_request(request)

        def __init__(self):
            limits = httpx_module.Limits()
            self._pool = httpcore_module.AsyncConnectionPool(
                ssl_context=config.create_ssl_context(verify=True, cert=None, trust_env=True),
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
                http1=True, http2=False, network_backend=backend,
            )

    return PinnedAsyncHTTPTransport()


def pinned_httpx_async_transport(validated: ValidatedOutboundURL):
    """Native httpx transport; preserve the URL host for Host, SNI and TLS."""
    import httpcore
    import httpx
    return _pinned_async_transport(validated, httpx_module=httpx, httpcore_module=httpcore)


def pinned_httpx2_async_transport(validated: ValidatedOutboundURL):
    """Native httpx2 transport for MCP 2; pin approved IPs without a DNS retry."""
    import httpcore2
    import httpx2
    return _pinned_async_transport(validated, httpx_module=httpx2, httpcore_module=httpcore2)
