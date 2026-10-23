# Modified by Kestrel: isolated SDK namespace; see VENDORED.json.
"""General utility functions for the A2A Python SDK."""

import functools
import inspect
import logging

from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar, cast

from packaging.version import InvalidVersion

from kestrel_sdk.native_a2a.server.context import ServerCallContext
from kestrel_sdk.native_a2a.utils import constants
from kestrel_sdk.native_a2a.utils.errors import VersionNotSupportedError
from kestrel_sdk.native_a2a.utils.versioning import major_minor


F = TypeVar('F', bound=Callable[..., Any])


logger = logging.getLogger(__name__)


def validate_version(expected_version: str) -> Callable[[F], F]:
    """Validate A2A-Version headers and query parameters in the context.

    The header name is defined by `constants.VERSION_HEADER` ('A2A-Version').
    Missing or empty versions mean `constants.PROTOCOL_VERSION_0_3` ('0.3').
    Every supplied value must match `expected_version` (major and minor parts),
    a `VersionNotSupportedError` is raised. Patch version is ignored.

    This decorator supports both async methods and async generator methods. It
    expects a `ServerCallContext` to be present either in the arguments or
    keyword arguments of the decorated method.

    Args:
        expected_version: The A2A protocol version string expected by the method.

    Returns:
        The decorated function.

    Raises:
        VersionNotSupportedError: If the version in the request does not match `expected_version`.
    """
    expected_release = major_minor(expected_version)

    def decorator(func: F) -> F:
        def _get_actual_versions(
            args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> tuple[str, ...]:
            context = kwargs.get('context')
            if context is None:
                for arg in args:
                    if isinstance(arg, ServerCallContext):
                        context = arg
                        break

            if context is None:
                # If no context is found, we can't validate the version.
                # In a real scenario, this shouldn't happen for properly routed requests.
                # We default to the expected version to allow test call to proceed.
                return (expected_version,)

            headers = context.state.get('headers', {})
            parameters = (
                *headers.items(),
                *context.state.get('query_params', ()),
            )
            versions = tuple(
                str(value) if value else constants.PROTOCOL_VERSION_0_3
                for name, value in parameters
                if name.casefold() == constants.VERSION_HEADER.casefold()
            )
            return versions or (constants.PROTOCOL_VERSION_0_3,)

        def _is_version_compatible(actual: str) -> bool:
            try:
                return major_minor(actual) == expected_release
            except InvalidVersion:
                return False

        def _validate_context_version(
            args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> None:
            for actual_version in _get_actual_versions(args, kwargs):
                if not _is_version_compatible(actual_version):
                    logger.warning(
                        'Version mismatch: actual=%r, expected=%r',
                        actual_version,
                        expected_version,
                    )
                    raise VersionNotSupportedError(
                        message=f"A2A version '{actual_version}' is not supported by this handler. "
                        f"Expected version '{expected_version}'."
                    )

        if inspect.isasyncgenfunction(inspect.unwrap(func)):

            @functools.wraps(func)
            def async_gen_wrapper(
                *args: Any, **kwargs: Any
            ) -> AsyncIterator[Any]:
                _validate_context_version(args, kwargs)
                return func(*args, **kwargs)

            return cast('F', async_gen_wrapper)

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            _validate_context_version(args, kwargs)
            return await func(*args, **kwargs)

        return cast('F', async_wrapper)

    return decorator
