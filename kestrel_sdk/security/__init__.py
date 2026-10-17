"""Kestrel SDK — Security interfaces, outbound policy and encryption helpers.

Outbound policy uses the standard library; native HTTP transports require the
matching 'httpx' or 'httpx2' extra. Encryption requires the 'crypto' extra.
"""

from .exceptions import (
    SecurityError,
    EncryptionError,
    DecryptionError,
    MasterKeyNotConfiguredError,
    InvalidPurposeError,
    KeyStorageError,
    KeyNotFoundError,
    KeyNotConfiguredError,
    PassphraseRequiredError,
)

__all__ = [
    "SecurityError",
    "EncryptionError",
    "DecryptionError",
    "MasterKeyNotConfiguredError",
    "InvalidPurposeError",
    "KeyStorageError",
    "KeyNotFoundError",
    "KeyNotConfiguredError",
    "PassphraseRequiredError",
]

# encryption submodule requires cryptography — import lazily
