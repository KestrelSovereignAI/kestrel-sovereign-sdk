# Modified by Kestrel: isolated SDK namespace; see VENDORED.json.
"""Explicit request roots spanning endpoint dispatch AND ASGI response use."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, Protocol, Self, TypeVar

from kestrel_sdk.native_a2a.utils.source_lifetime import (
    SourceDemand,
    SourceFactory,
    SourceLifetimeFactory,
    standalone_source_lifetime,
)


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from types import TracebackType

    from starlette.datastructures import FormData
    from starlette.requests import AwaitableOrContextManager, Request
    from starlette.responses import Response
    from starlette.routing import Route
    from starlette.types import ASGIApp, Receive, Scope, Send
else:
    try:
        from starlette.requests import Request
    except ImportError:
        Request = Any


T = TypeVar('T')


class SourceRequestRoot:
    """Own one streaming source until dispatch AND response execution finish.

    A root is created per request. Its factory is an explicit policy choice,
    never discovered from ContextVars, request.scope or a reader. Standalone
    applications use AnyIO; hosts with native admission pass their own factory.
    """

    def __init__(self, factory: SourceLifetimeFactory) -> None:
        self._factory = factory
        self._stack = AsyncExitStack()
        self._active = False
        self._entered = False
        self._opened = False
        self._reader: SourceDemand[Any] | None = None

    async def __aenter__(self) -> Self:
        """Enter a fresh single-use request root before endpoint dispatch."""
        if self._entered:
            raise RuntimeError('A source request root cannot be re-entered')
        self._entered = True
        await self._stack.__aenter__()
        self._active = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Retire the admitted source before the original request root exits."""
        try:
            return await self._stack.__aexit__(exc_type, exc_value, traceback)
        finally:
            self._active = False

    async def open(self, acquire: SourceFactory[T]) -> SourceDemand[T]:
        """Admit exactly one source; its factory runs only in that owner."""
        if not self._active:
            raise RuntimeError(
                'Streaming dispatch requires an active source root'
            )
        if self._opened:
            raise RuntimeError('A request source has already been admitted')
        self._opened = True
        reader = await self._stack.enter_async_context(self._factory(acquire))
        self._reader = reader
        return reader

    def raise_if_retirement_failed(self) -> None:
        """Keep a known retirement failure out of a normalized HTTP response."""
        if self._reader is not None:
            self._reader.raise_if_retirement_failed()


class SourceEndpoint(Protocol):
    """A dispatcher accepting the actual request root explicitly."""

    async def __call__(self, request: Request) -> Response:
        """Dispatch without moving ownership into response consumers."""
        ...


class SourceRequest(Request):
    """A request explicitly given its selected root, not scope authority."""

    def __init__(
        self, request: Request, source_root: SourceRequestRoot
    ) -> None:
        super().__init__(request.scope, receive=request.receive)
        self._original_request = request
        self._source_root = source_root

    async def body(self) -> bytes:
        """Preserve the original request's cache and transport-consumption state."""
        return await self._original_request.body()

    async def json(self) -> Any:
        """Use the original parser/cache, not a second JSON request body."""
        return await self._original_request.json()

    def stream(self) -> AsyncGenerator[bytes, None]:
        """Borrow the original body stream without another transport consumer."""
        return self._original_request.stream()

    def form(self, **kwargs: Any) -> AwaitableOrContextManager[FormData]:
        """Preserve the original form cache and its library-selected options."""
        return self._original_request.form(**kwargs)

    async def close(self) -> None:
        """Retire the original request's form resources, without a copied cache."""
        await self._original_request.close()

    async def send_push_promise(self, path: str) -> None:
        """Use the original server send capability, not a missing replacement."""
        await self._original_request.send_push_promise(path)

    async def is_disconnected(self) -> bool:
        """Preserve the original disconnect cache without consuming receive twice."""
        return await self._original_request.is_disconnected()

    @property
    def source_root(self) -> SourceRequestRoot:
        """Return the caller-selected root; it still must admit the source."""
        return self._source_root


def source_root_for(request: Request) -> SourceRequestRoot:
    """Require explicit root injection, never reconstruct it from scope."""
    if not isinstance(request, SourceRequest):
        raise TypeError(
            'Streaming dispatch requires SourceRequest with an active root; '
            'use source_request_response or explicitly enclose dispatch and '
            'response execution in SourceRequestRoot'
        )
    return request.source_root


def source_request_response(
    endpoint: SourceEndpoint,
    factory: SourceLifetimeFactory = standalone_source_lifetime,
) -> ASGIApp:
    """Use Starlette's normal dispatcher inside the complete source root."""
    from starlette.routing import (  # noqa: PLC0415 -- optional HTTP extra
        request_response,
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        async with SourceRequestRoot(factory) as root:

            async def dispatch(request: Request) -> Response:
                response = await endpoint(SourceRequest(request, root))
                # An early protocol normalizer may have caught a body error.
                # It must not turn a failed original close into a JSON success
                # before headers. The policy still joins pending work on exit.
                root.raise_if_retirement_failed()
                return response

            await request_response(dispatch)(scope, receive, send)

    return app


def create_source_route(
    path: str,
    endpoint: SourceEndpoint,
    *,
    methods: list[str],
    factory: SourceLifetimeFactory = standalone_source_lifetime,
) -> Route:
    """Retain Route metadata while installing its complete request owner."""
    from starlette.routing import Route  # noqa: PLC0415 -- optional extra

    return with_source_lifetime(
        Route(path=path, endpoint=endpoint, methods=methods), factory
    )


def with_source_lifetime(
    route: Route,
    factory: SourceLifetimeFactory = standalone_source_lifetime,
) -> Route:
    """Install the one request-root wrapper before mounting a fresh route."""
    route.app = source_request_response(route.endpoint, factory)
    return route
