"""Shared outbound URL validation and DNS-pinned HTTP transports.

The policy uses IDNA 2008 for DNS and wire identity. Install the matching ``httpx``
or ``httpx2`` SDK extra to construct that client's native transport. Validate
before connecting; a transport is bound to that one origin and its approved
IP set. Redirect destinations require separate validation and a new transport.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib
from importlib.resources import files
import ipaddress
import socket
import tomllib
from types import ModuleType
from typing import Any
from urllib.parse import urlparse

import idna

from .exceptions import SSRFError


_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _load_non_global_networks():
    with files(__package__).joinpath("outbound_policy.toml").open("rb") as source:
        policy = tomllib.load(source)
    return tuple(ipaddress.ip_network(prefix) for prefix in policy["non_global_networks"])


_NON_GLOBAL_NETWORKS = _load_non_global_networks()


def _origin_port(scheme: str, port: int | None) -> int:
    if port is not None:
        return port
    if scheme not in _DEFAULT_PORTS:
        raise SSRFError("URL scheme requires an explicit port")
    return _DEFAULT_PORTS[scheme]


@dataclass(frozen=True)
class ValidatedOutboundURL:
    """One resolved origin whose approved IPs must be used for connection."""

    url: str
    scheme: str
    host: str
    port: int
    ip_addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]

    def __post_init__(self) -> None:
        try:
            addresses = tuple(ipaddress.ip_address(address) for address in self.ip_addresses)
        except (TypeError, ValueError):
            raise SSRFError("validated outbound URL contains an invalid address") from None
        if not addresses:
            raise SSRFError("validated outbound URL has no addresses")
        for address in addresses:
            if _address_is_blocked(address):
                raise SSRFError("URL resolves to a non-public address")
        url, _scheme, host, port = _canonical_url_origin(self.url, (self.scheme,))
        if not _origin_hosts_match(self.host, host) or self.port != port:
            raise SSRFError("validated outbound URL origin does not match its metadata")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and any(address != literal for address in addresses):
            raise SSRFError("validated IP-literal URL must approve only its own address")
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "ip_addresses", addresses)

    def assert_origin(self, host: str, port: int) -> None:
        """Refuse a request that escapes the origin whose DNS was validated."""
        if not _origin_hosts_match(host, self.host) or port != self.port:
            raise SSRFError("pinned transport refused unexpected origin")


def _address_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address that must not be reachable from a server-side fetch.

    ``is_private`` already covers RFC1918, loopback, link-local (169.254/16 — the
    cloud-metadata range), unique-local IPv6 (fd00::/8), and more on modern
    Python; we add the remaining non-global categories explicitly for clarity and
    forward-compatibility.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        # Local interface/zone hints do not identify a public remote origin.
        # Check before IPv4 mapping, which would discard the scope identifier.
        if ip.scope_id is not None:
            return True
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)
        or not ip.is_global
        or any(ip.version == network.version and ip in network for network in _NON_GLOBAL_NETWORKS)
    )


def _resolve_addresses(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve ``host`` to all IPs (blocking). Raises SSRFError on failure."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as e:
        raise SSRFError(f"cannot resolve host {host!r}: {e}") from e
    return list(dict.fromkeys(ipaddress.ip_address(info[4][0]) for info in infos))


def _canonical_origin_host(host: str) -> str:
    """Require an IP literal or IDNA DNS name and return its wire identity."""
    bracketed = host.startswith("[") and host.endswith("]")
    if bracketed:
        host = host[1:-1]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if bracketed and not isinstance(address, ipaddress.IPv6Address):
        raise SSRFError("outbound bracketed origin is not a supported IPv6 literal")
    if address is not None:
        return str(address).casefold()
    try:
        canonical = idna.encode(host.lower()).decode("ascii").casefold()
    except UnicodeError:
        raise SSRFError("outbound origin host is not a valid IDNA name") from None
    # Native HTTP clients treat four numeric labels as an IPv4 literal,
    # including when IDNA mapped Unicode separators into ASCII dots.
    labels = canonical.split(".")
    if len(labels) == 4 and all(label.isdigit() for label in labels):
        try:
            return str(ipaddress.IPv4Address(canonical))
        except ValueError:
            raise SSRFError("outbound origin host is not a valid IP literal") from None
    return canonical


def _origin_hosts_match(candidate: str, validated: str) -> bool:
    return _canonical_origin_host(candidate) == _canonical_origin_host(validated)


def _canonical_url_origin(url: str, allowed_schemes: tuple[str, ...]) -> tuple[str, str, str, int]:
    """Canonicalize the authority without changing the raw request suffix."""
    if not isinstance(url, str):
        raise SSRFError("outbound URL must be a string")
    if url != url.strip() or any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise SSRFError("outbound URL contains control characters or surrounding whitespace")
    try:
        url.encode("utf-8")
    except UnicodeEncodeError:
        raise SSRFError("outbound URL must be encodable as UTF-8") from None
    # Malformed URLs (bad port, invalid IPv6 literal, …) make urlparse/.port
    # raise ValueError; convert to SSRFError so callers get a clean rejection
    # (400) instead of an uncaught 500 (codex r1).
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        # Parser and IDNA exceptions can include an unbounded raw authority.
        # Expected input rejections must remain bounded in logged tracebacks too.
        raise SSRFError("malformed outbound URL authority") from None

    if scheme not in allowed_schemes:
        raise SSRFError("outbound URL scheme is not allowed")
    if not host:
        raise SSRFError("outbound URL has no host")
    if parsed.username is not None or parsed.password is not None:
        raise SSRFError("outbound URL must not contain user information")
    # Preserve the authority's host kind. urllib also parses IPvFuture, which
    # must not silently become an unrelated DNS name when brackets are removed.
    host = _canonical_origin_host(f"[{host}]" if parsed.netloc.startswith("[") else host)
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    port = _origin_port(scheme, port)
    # urlunparse drops empty query/fragment delimiters. Replace only the
    # parsed authority so validation cannot change a signed request target.
    prefix, separator, remainder = url.partition("//")
    canonical_url = prefix + separator + authority + remainder[len(parsed.netloc):]
    return canonical_url, scheme, host, port


def validate_outbound_url(
    url: str, *, allowed_schemes: "tuple[str, ...]" = ("http", "https")
) -> ValidatedOutboundURL:
    """Validate and canonicalize a URL for a server-side fetch.

    DNS uses the same IDNA 2008 ASCII host as the returned ``url`` and ``host``.
    Synchronous (uses ``socket.getaddrinfo``); prefer :func:`assert_safe_url` in
    async code. Fetchers must use the returned URL and approved IPs rather than
    resolving the original input again. Unsafe input raises :class:`SSRFError`.
    """
    url, scheme, host, port = _canonical_url_origin(url, allowed_schemes)

    # A rejected literal is not a hostname-resolution failure. Do not catch
    # our own SSRFError (a ValueError) and repeat DNS for a blocked address.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return ValidatedOutboundURL(
            url=url, scheme=scheme, host=host, port=port, ip_addresses=(ip,)
        )

    resolved = _resolve_addresses(host, port)
    if not resolved:
        raise SSRFError(f"cannot resolve host {host!r}: no addresses returned")
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
                 connect_errors: tuple[type[Exception], ...],
                 connect_timeout_error: type[Exception] = TimeoutError):
        self._validated = validated
        self._backend = backend
        self._connect_errors = connect_errors
        self._connect_timeout_error = connect_timeout_error

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None, socket_options: Any = None):
        self._validated.assert_origin(host, port)
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        try:
            async with asyncio.timeout(timeout):
                for index, address in enumerate(self._validated.ip_addresses):
                    remaining = None if deadline is None else max(0.0, deadline - loop.time())
                    try:
                        return await self._backend.connect_tcp(
                            host=str(address), port=port, timeout=remaining,
                            local_address=local_address, socket_options=socket_options,
                        )
                    except self._connect_errors:
                        if index == len(self._validated.ip_addresses) - 1:
                            raise
        except TimeoutError as error:
            raise self._connect_timeout_error("approved-address connection deadline exceeded") from error

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Any = None):
        raise SSRFError("pinned transport does not allow Unix-socket connections")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def _pinned_async_transport(validated: ValidatedOutboundURL, *,
                            httpx_module: ModuleType, httpcore_module: ModuleType):
    config = importlib.import_module(f"{httpx_module.__name__}._config")
    backend = _PinnedAsyncNetworkBackend(
        validated, backend=httpcore_module.AnyIOBackend(),
        connect_errors=(httpcore_module.NetworkError, httpcore_module.TimeoutException, OSError),
        connect_timeout_error=httpcore_module.ConnectTimeout,
    )

    class PinnedAsyncHTTPTransport(httpx_module.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            if request.url.scheme != validated.scheme:
                raise SSRFError("pinned transport refused a changed URL scheme")
            port = request.url.port
            validated.assert_origin(
                request.url.raw_host.decode("ascii"),
                _origin_port(request.url.scheme, port),
            )
            return await super().handle_async_request(request)

        def __init__(self):
            limits = config.DEFAULT_LIMITS
            self._pool = httpcore_module.AsyncConnectionPool(
                ssl_context=httpx_module.create_ssl_context(verify=True, cert=None, trust_env=True),
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
