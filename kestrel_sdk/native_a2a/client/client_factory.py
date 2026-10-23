# Modified by Kestrel: isolated SDK namespace; see VENDORED.json.
from __future__ import annotations

import logging

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from packaging.version import InvalidVersion, Version

from kestrel_sdk.native_a2a.client.base_client import BaseClient
from kestrel_sdk.native_a2a.client.card_resolver import A2ACardResolver
from kestrel_sdk.native_a2a.client.client import Client, ClientConfig
from kestrel_sdk.native_a2a.client.transports.base import ClientTransport
from kestrel_sdk.native_a2a.client.transports.jsonrpc import JsonRpcTransport
from kestrel_sdk.native_a2a.client.transports.rest import RestTransport
from kestrel_sdk.native_a2a.client.transports.tenant_decorator import TenantTransportDecorator
from kestrel_sdk.native_a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
)
from kestrel_sdk.native_a2a.utils.constants import (
    PROTOCOL_VERSION_1_0,
    PROTOCOL_VERSION_CURRENT,
    VERSION_HEADER,
    TransportProtocol,
)


if TYPE_CHECKING:
    from kestrel_sdk.native_a2a.client.interceptors import ClientCallInterceptor


try:
    from kestrel_sdk.native_a2a.client.transports.grpc import GrpcTransport
except ImportError:
    GrpcTransport = None  # type: ignore # pyright: ignore


logger = logging.getLogger(__name__)


TransportProducer = Callable[
    [AgentCard, str, ClientConfig],
    ClientTransport,
]


def major_minor(version: str) -> tuple[int, int]:
    """Returns the `Major.Minor` release of a protocol version."""
    v = Version(version)
    return (v.major, v.minor)


class ClientFactory:
    """Factory for creating clients that communicate with A2A agents.

    The factory is configured with a `ClientConfig` and optionally custom
    transport producers registered via `register`. Example usage:

        factory = ClientFactory(config)
        # Optionally register custom transport implementations
        factory.register('my_custom_transport', custom_transport_producer)
        # Create a client from an AgentCard
        client = factory.create(card, interceptors)
        # Or resolve an AgentCard from a URL and create a client
        client = await factory.create_from_url('https://example.com')

    The client can be used consistently regardless of the transport. This
    aligns the client configuration with the server's capabilities.

    Each created client gets its own `httpx.AsyncClient` and closes it on
    `close()`, unless `ClientConfig.httpx_client` is set.
    """

    def __init__(
        self,
        config: ClientConfig | None = None,
    ):
        config = config or ClientConfig()
        if config.httpx_client:
            config.httpx_client.headers.setdefault(
                VERSION_HEADER, PROTOCOL_VERSION_CURRENT
            )

        self._config = config
        self._registry: dict[str, TransportProducer] = {}
        self._register_defaults(config.supported_protocol_bindings)

    def _resolve_http_client(self) -> tuple[httpx.AsyncClient, bool]:
        """Returns an HTTP client and whether it is shared across clients."""
        if self._config.httpx_client:
            return self._config.httpx_client, True
        httpx_client = httpx.AsyncClient()
        httpx_client.headers.setdefault(
            VERSION_HEADER, PROTOCOL_VERSION_CURRENT
        )
        return httpx_client, False

    def _register_defaults(self, supported: list[str]) -> None:
        # Empty support list implies JSON-RPC only.

        if TransportProtocol.JSONRPC in supported or not supported:

            def jsonrpc_transport_producer(
                card: AgentCard,
                url: str,
                config: ClientConfig,
            ) -> ClientTransport:
                httpx_client, is_shared_httpx = self._resolve_http_client()

                return JsonRpcTransport(
                    httpx_client,
                    card,
                    url,
                    is_shared_httpx=is_shared_httpx,
                )

            self.register(
                TransportProtocol.JSONRPC,
                jsonrpc_transport_producer,
            )
        if TransportProtocol.HTTP_JSON in supported:

            def rest_transport_producer(
                card: AgentCard,
                url: str,
                config: ClientConfig,
            ) -> ClientTransport:
                httpx_client, is_shared_httpx = self._resolve_http_client()

                return RestTransport(
                    httpx_client,
                    card,
                    url,
                    is_shared_httpx=is_shared_httpx,
                )

            self.register(
                TransportProtocol.HTTP_JSON,
                rest_transport_producer,
            )
        if TransportProtocol.GRPC in supported:
            if GrpcTransport is None:
                raise ImportError(
                    'To use GrpcClient, its dependencies must be installed. '
                    'You can install them with \'pip install "a2a-sdk[grpc]"\''
                )

            _grpc_transport = GrpcTransport

            def grpc_transport_producer(
                card: AgentCard,
                url: str,
                config: ClientConfig,
            ) -> ClientTransport:
                return _grpc_transport.create(card, url, config)

            self.register(
                TransportProtocol.GRPC,
                grpc_transport_producer,
            )

    @staticmethod
    def _find_best_interface(
        interfaces: list[AgentInterface],
        protocol_bindings: list[str] | None = None,
        url: str | None = None,
    ) -> AgentInterface | None:
        """Finds the best interface based on protocol version priorities."""
        candidates = [
            i
            for i in interfaces
            if (
                protocol_bindings is None
                or i.protocol_binding in protocol_bindings
            )
            and (url is None or i.url == url)
        ]

        if not candidates:
            return None

        # This maintained native client supports A2A 1.0, including patch
        # revisions. Missing versions are only useful for minimal bootstrap
        # cards; an advertised unsupported version must never select 0.3 code.
        native_version = major_minor(PROTOCOL_VERSION_1_0)
        unversioned = None
        for interface in candidates:
            if not interface.protocol_version:
                if unversioned is None:
                    unversioned = interface
                continue
            try:
                if major_minor(interface.protocol_version) == native_version:
                    return interface
            except InvalidVersion:
                continue
        return unversioned

    async def create_from_url(
        self,
        url: str,
        interceptors: list[ClientCallInterceptor] | None = None,
        relative_card_path: str | None = None,
        resolver_http_kwargs: dict[str, Any] | None = None,
        signature_verifier: Callable[[AgentCard], None] | None = None,
    ) -> Client:
        """Create a `Client` by resolving an `AgentCard` from a URL.

        Resolves the agent card from the given URL, then creates a client
        via `create`. The card is fetched with `ClientConfig.httpx_client`
        when one is set, otherwise with a temporary client.

        If the agent card is already available, use `create` directly
        instead.

        Args:
          url: The base URL of the agent. The agent card will be fetched
            from `<url>/.well-known/agent-card.json` by default.
          interceptors: A list of interceptors to use for each request.
            These are used for things like attaching credentials or http
            headers to all outbound requests.
          relative_card_path: The relative path when resolving the agent
            card. See `A2ACardResolver.get_agent_card` for details.
          resolver_http_kwargs: Dictionary of arguments to provide to the
            httpx client when resolving the agent card.
          signature_verifier: A callable used to verify the agent card's
            signatures.

        Returns:
          A `Client` object.
        """
        httpx_client, is_shared_httpx = self._resolve_http_client()
        try:
            resolver = A2ACardResolver(httpx_client, url)
            card = await resolver.get_agent_card(
                relative_card_path=relative_card_path,
                http_kwargs=resolver_http_kwargs,
                signature_verifier=signature_verifier,
            )
        finally:
            if not is_shared_httpx:
                await httpx_client.aclose()
        return self.create(card, interceptors)

    def register(self, label: str, generator: TransportProducer) -> None:
        """Register a new transport producer for a given transport label."""
        self._registry[label] = generator

    def create(
        self,
        card: AgentCard,
        interceptors: list[ClientCallInterceptor] | None = None,
    ) -> Client:
        """Create a new `Client` for the provided `AgentCard`.

        Args:
          card: An `AgentCard` defining the characteristics of the agent.
          interceptors: A list of interceptors to use for each request. These
            are used for things like attaching credentials or http headers
            to all outbound requests.

        Returns:
          A `Client` object.

        Raises:
          If there is no valid matching of the client configuration with the
          server configuration, a `ValueError` is raised.
        """
        client_set = self._config.supported_protocol_bindings or [
            TransportProtocol.JSONRPC
        ]
        transport_protocol = None
        selected_interface = None
        if self._config.use_client_preference:
            for protocol_binding in client_set:
                selected_interface = ClientFactory._find_best_interface(
                    list(card.supported_interfaces),
                    protocol_bindings=[protocol_binding],
                )
                if selected_interface:
                    transport_protocol = protocol_binding
                    break
        else:
            for supported_interface in card.supported_interfaces:
                if supported_interface.protocol_binding in client_set:
                    transport_protocol = supported_interface.protocol_binding
                    selected_interface = ClientFactory._find_best_interface(
                        list(card.supported_interfaces),
                        protocol_bindings=[transport_protocol],
                    )
                    if selected_interface:
                        break
        if not transport_protocol or not selected_interface:
            raise ValueError('no compatible transports found.')
        if transport_protocol not in self._registry:
            raise ValueError(f'no client available for {transport_protocol}')

        transport = self._registry[transport_protocol](
            card, selected_interface.url, self._config
        )

        if selected_interface.tenant:
            transport = TenantTransportDecorator(
                transport, selected_interface.tenant
            )

        return BaseClient(
            card,
            self._config,
            transport,
            interceptors or [],
        )


async def create_client(  # noqa: PLR0913
    agent: str | AgentCard,
    client_config: ClientConfig | None = None,
    interceptors: list[ClientCallInterceptor] | None = None,
    relative_card_path: str | None = None,
    resolver_http_kwargs: dict[str, Any] | None = None,
    signature_verifier: Callable[[AgentCard], None] | None = None,
) -> Client:
    """Create a `Client` for an agent from a URL or `AgentCard`.

    Convenience function that constructs a `ClientFactory` internally.
    For reusing a factory across multiple agents or registering custom
    transports, use `ClientFactory` directly instead.

    Args:
      agent: The base URL of the agent, or an `AgentCard` to use
        directly.
      client_config: Optional `ClientConfig`. A default config is
        created if not provided.
      interceptors: A list of interceptors to use for each request.
      relative_card_path: The relative path when resolving the agent
        card. Only used when `agent` is a URL.
      resolver_http_kwargs: Dictionary of arguments to provide to the
        httpx client when resolving the agent card.
      signature_verifier: A callable used to verify the agent card's
        signatures.

    Returns:
      A `Client` object.
    """
    factory = ClientFactory(client_config)
    if isinstance(agent, str):
        return await factory.create_from_url(
            agent,
            interceptors=interceptors,
            relative_card_path=relative_card_path,
            resolver_http_kwargs=resolver_http_kwargs,
            signature_verifier=signature_verifier,
        )
    return factory.create(agent, interceptors)


def minimal_agent_card(
    url: str, transports: list[str] | None = None
) -> AgentCard:
    """Generates a minimal card to simplify bootstrapping client creation.

    This minimal card is not viable itself to interact with the remote agent.
    Instead this is a shorthand way to take a known url and transport option
    and interact with the get card endpoint of the agent server to get the
    correct agent card. This pattern is necessary for gRPC based card access
    as typically these servers won't expose a well known path card.
    """
    if transports is None:
        transports = []
    return AgentCard(
        supported_interfaces=[
            AgentInterface(protocol_binding=t, url=url) for t in transports
        ],
        capabilities=AgentCapabilities(extended_agent_card=True),
        default_input_modes=[],
        default_output_modes=[],
        description='',
        skills=[],
        version='',
        name='',
    )
