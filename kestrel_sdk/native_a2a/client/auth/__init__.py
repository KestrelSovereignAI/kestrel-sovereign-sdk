# Modified by Kestrel: isolated SDK namespace; see VENDORED.json.
"""Client-side authentication components for the A2A Python SDK."""

from kestrel_sdk.native_a2a.client.auth.credentials import (
    CredentialService,
    InMemoryContextCredentialStore,
)
from kestrel_sdk.native_a2a.client.auth.interceptor import AuthInterceptor


__all__ = [
    'AuthInterceptor',
    'CredentialService',
    'InMemoryContextCredentialStore',
]
