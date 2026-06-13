"""
Symmetric encryption helpers for Kestrel SDK.

Requires the 'crypto' extra: pip install kestrel-sovereign-sdk[crypto]

Wave 0C (#915) of the Quantum Hardening epic (#921): the AEAD primitive
moved from Fernet (AES-128-CBC + HMAC-SHA256) to AES-256-GCM via
``AEADCipher``. AEADCipher is a drop-in replacement for ``Fernet`` —
``.encrypt()``/``.decrypt()`` API preserved — so call-sites need not
change. Existing Fernet ciphertext continues to decrypt because
``AEADCipher.decrypt`` dispatches on token prefix; new writes always
emit the v2 ``KSAv2:`` token. See ``kestrel_sdk/security/aead.py``.

The legacy public names ``get_fernet``, ``get_agent_fernet``,
``encrypt_string_fernet``, ``decrypt_string_fernet`` keep their behaviour
contract; they now return / accept ``AEADCipher`` instances instead of
``Fernet`` instances. Type annotations updated accordingly.

Provides:
- Per-agent key derivation (each agent gets unique keys)
- Purpose-specific subkeys (conversations, service-keys, wallet, backup)
- Multiple key sources (env var, Docker Secrets, file paths)
- Explicit error handling (no silent failures)

Key Hierarchy:
    KESTREL_DATA_KEY (env var or secrets file)
        | (PBKDF2-HMAC-SHA256 if passphrase)
    Master Key (32-byte raw key, base64-encodable for legacy Fernet shape)
        | (HKDF with agent DID as salt)
    Agent Key
        | (HKDF with purpose as info)
        |-- kestrel-conversations-v1
        |-- kestrel-service-keys-v1
        |-- kestrel-wallet-v1
        +-- kestrel-backup-v1
"""
import hashlib
import base64
import logging
import os
from pathlib import Path
from typing import Optional, Dict, Tuple, Any

from cryptography.fernet import Fernet  # validation-only: Fernet still used to detect raw-Fernet-key shape
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

from .aead import AEADCipher
from .exceptions import (
    DecryptionError,
    MasterKeyNotConfiguredError,
    InvalidPurposeError,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

ENV_VAR_NAME = "KESTREL_DATA_KEY"
KEY_SIZE = 32  # 256 bits
NONCE_SIZE = 12  # 96 bits (legacy AES-GCM, kept for compatibility)
PASSPHRASE_KDF_ITERATIONS = 600_000
PASSPHRASE_SALT_SIZE = 32
SALT_ENV_VAR_NAME = "KESTREL_DATA_KEY_SALT_FILE"
DEFAULT_SALT_FILE = ".kestrel/kestrel_data_key.salt"

# Valid purposes for purpose-specific encryption
VALID_PURPOSES = frozenset([
    "conversations",
    "service-keys",
    "wallet",
    "backup",
])


# =============================================================================
# Key Loading
# =============================================================================

def _read_key_from_file(path: str) -> Optional[str]:
    """Read a key from a secrets file (Docker Secrets, Kubernetes Secrets, etc.)."""
    try:
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                return f.read().strip()
    except Exception as e:
        logger.warning(f"Could not read key from {path}: {e}")
    return None


def _strip_quotes(value: str) -> str:
    """Strip surrounding quotes from a value.

    Docker's --env-file includes quotes literally, while python-dotenv strips them.
    This ensures consistent key values regardless of how env vars are loaded.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _get_data_key() -> Optional[str]:
    """
    Get the KESTREL_DATA_KEY from multiple sources in order of preference:

    1. KESTREL_DATA_KEY_FILE - Path to a secrets file (Docker/K8s secrets)
    2. /run/secrets/kestrel_data_key - Default Docker Secrets location
    3. KESTREL_DATA_KEY - Environment variable (legacy, less secure)

    Using file-based secrets prevents key exposure via `docker inspect`.
    """
    # Priority 1: Explicit file path
    key_file = os.environ.get("KESTREL_DATA_KEY_FILE")
    if key_file:
        key = _read_key_from_file(key_file)
        if key:
            logger.debug("Loaded key from KESTREL_DATA_KEY_FILE")
            return _strip_quotes(key)

    # Priority 2: Default Docker Secrets path
    docker_secret_path = "/run/secrets/kestrel_data_key"
    if os.path.exists(docker_secret_path):
        key = _read_key_from_file(docker_secret_path)
        if key:
            logger.debug("Loaded key from Docker Secret")
            return _strip_quotes(key)

    # Priority 3: Environment variable (legacy)
    env_key = os.environ.get(ENV_VAR_NAME)
    if env_key:
        # Warn about ENV usage if running in Docker
        if os.path.exists("/.dockerenv"):
            logger.warning(
                "KESTREL_DATA_KEY in ENV is insecure in Docker. "
                "Use Docker Secrets: --secret kestrel_data_key or mount to /run/secrets/"
            )
        return _strip_quotes(env_key)

    return None


def _get_configured_key_file() -> Optional[str]:
    """Return the configured key-file path only when it provides the data key."""
    key_file = os.environ.get("KESTREL_DATA_KEY_FILE")
    if key_file and _read_key_from_file(key_file):
        return key_file
    return None


def _get_passphrase_salt_path() -> Path:
    """Return the deterministic salt-file path for passphrase-derived keys."""
    explicit = os.environ.get(SALT_ENV_VAR_NAME)
    if explicit:
        return Path(explicit).expanduser()

    key_file = _get_configured_key_file()
    if key_file:
        return Path(key_file).expanduser().with_name(Path(key_file).name + ".salt")

    return Path.home() / DEFAULT_SALT_FILE


def _load_or_create_passphrase_salt() -> bytes:
    """Load or persist the non-secret salt used for passphrase KDF."""
    salt_path = _get_passphrase_salt_path()
    if salt_path.is_file():
        encoded = salt_path.read_text(encoding="utf-8").strip()
        salt = base64.urlsafe_b64decode(encoded.encode("ascii"))
        if len(salt) != PASSPHRASE_SALT_SIZE:
            raise ValueError(
                f"{salt_path} must contain a base64-encoded {PASSPHRASE_SALT_SIZE}-byte salt"
            )
        return salt

    salt = os.urandom(PASSPHRASE_SALT_SIZE)
    salt_path.parent.mkdir(parents=True, exist_ok=True)
    salt_path.write_text(base64.urlsafe_b64encode(salt).decode("ascii"), encoding="utf-8")
    try:
        salt_path.chmod(0o600)
    except OSError:
        logger.debug("Could not chmod passphrase salt file %s", salt_path)
    return salt


def _is_fernet_key(key: str) -> bool:
    """Return True when key is already a raw Fernet/AEAD key value."""
    try:
        Fernet(key.encode("ascii"))
        return True
    except Exception:
        return False


def _derive_passphrase_master_key(key: str) -> bytes:
    """Derive the current salted PBKDF2 master key from a passphrase."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=_load_or_create_passphrase_salt(),
        iterations=PASSPHRASE_KDF_ITERATIONS,
    )
    return kdf.derive(key.encode("utf-8"))


def _derive_legacy_passphrase_master_key(key: str) -> bytes:
    """Derive the pre-#26 unsalted SHA-256 master key for read fallback."""
    return hashlib.sha256(key.encode("utf-8")).digest()


class _ReadFallbackCipher:
    """Cipher wrapper that writes with the current key and reads legacy data."""

    __slots__ = ("_primary", "_legacy")

    def __init__(self, primary: AEADCipher, legacy: AEADCipher):
        self._primary = primary
        self._legacy = legacy

    def encrypt(self, plaintext: bytes | str, aad: Optional[bytes] = None) -> bytes:
        return self._primary.encrypt(plaintext, aad=aad)

    def decrypt(self, token: bytes | str, aad: Optional[bytes] = None) -> bytes:
        try:
            return self._primary.decrypt(token, aad=aad)
        except DecryptionError:
            return self._legacy.decrypt(token, aad=aad)


def _get_master_key() -> bytes:
    """
    Get the master key, raising if not configured.

    Returns:
        32-byte master key

    Raises:
        MasterKeyNotConfiguredError: If KESTREL_DATA_KEY not set
    """
    key = _get_data_key()
    if not key:
        raise MasterKeyNotConfiguredError(
            f"{ENV_VAR_NAME} environment variable is not set. "
            "Encryption requires this to be configured."
        )

    if _is_fernet_key(key):
        # Preserve the historical purpose-key behavior for raw Fernet-shaped
        # KESTREL_DATA_KEY values; only passphrases move to PBKDF2.
        return _derive_legacy_passphrase_master_key(key)

    return _derive_passphrase_master_key(key)


def _get_legacy_master_key() -> bytes:
    """Get the legacy unsalted-SHA-256 master key for read fallback."""
    key = _get_data_key()
    if not key:
        raise MasterKeyNotConfiguredError(
            f"{ENV_VAR_NAME} environment variable is not set. "
            "Encryption requires this to be configured."
        )
    return _derive_legacy_passphrase_master_key(key)


# =============================================================================
# AEAD Cipher Construction (Global and Per-Agent)
# =============================================================================
#
# Names preserved for caller compatibility: a returned AEADCipher acts as a
# drop-in for a Fernet (matching .encrypt()/.decrypt() shape) and, on
# decrypt, dispatches on the token prefix so legacy Fernet data still works.

def get_fernet() -> Optional[AEADCipher]:
    """
    Initialize the global symmetric AEAD cipher from KESTREL_DATA_KEY.

    Despite the legacy name, returns an ``AEADCipher`` (AES-256-GCM with
    Fernet read-compat) — drop-in for the previous ``Fernet`` instance.

    Returns:
        AEADCipher if key is available, None otherwise.
    """
    key = _get_data_key()
    if not key:
        return None

    if _is_fernet_key(key):
        return AEADCipher(key)

    logger.debug("Key is not a raw Fernet key, deriving from passphrase")
    primary = AEADCipher(_derive_passphrase_master_key(key))
    legacy = AEADCipher(_derive_legacy_passphrase_master_key(key))
    return _ReadFallbackCipher(primary, legacy)  # type: ignore[return-value]


def get_master_key_bytes() -> Optional[bytes]:
    """
    Get the master encryption key as bytes (URL-safe-base64 form).

    Returns the 44-byte URL-safe-base64 encoding of the 32-byte master
    key. Kept in this shape for callers (legacy and new) that pass it
    around as a Fernet-compatible key value; ``AEADCipher`` accepts
    either raw 32 bytes or this 44-byte form, so passing it through is
    safe in either direction.

    Returns:
        44 bytes, or None if key not available.
    """
    key = _get_data_key()
    if not key:
        return None

    if _is_fernet_key(key):
        key_bytes = key.encode()
        return key_bytes

    logger.debug("Key is not a raw Fernet key, deriving bytes from passphrase")
    return base64.urlsafe_b64encode(_derive_passphrase_master_key(key))


def _get_legacy_master_key_bytes() -> Optional[bytes]:
    """Get legacy passphrase master bytes for read fallback."""
    key = _get_data_key()
    if not key:
        return None
    if _is_fernet_key(key):
        return key.encode()
    return base64.urlsafe_b64encode(_derive_legacy_passphrase_master_key(key))


def _derive_agent_cipher_key(master: bytes, agent_id: str) -> bytes:
    """Derive the get_agent_fernet per-agent key from master bytes."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=agent_id.encode("utf-8"),
        info=b"kestrel-agent-v1",
    )
    return hkdf.derive(master)


def get_agent_fernet(agent_id: str) -> Optional[AEADCipher]:
    """
    Get an AEAD cipher with per-agent derived key using HKDF.

    Despite the legacy name, returns an ``AEADCipher``.

    Args:
        agent_id: Agent's DID (e.g., "did:pkh:eip155:1:0x...")

    Returns:
        AEADCipher with agent-specific key, or None if no master key.
    """
    master = get_master_key_bytes()
    if not master:
        return None

    if not agent_id:
        logger.warning("get_agent_fernet called with empty agent_id, falling back to global key")
        return get_fernet()

    try:
        derived = _derive_agent_cipher_key(master, agent_id)
        cipher = AEADCipher(derived)
        legacy_master = _get_legacy_master_key_bytes()
        if legacy_master and legacy_master != master:
            legacy = AEADCipher(_derive_agent_cipher_key(legacy_master, agent_id))
            return _ReadFallbackCipher(cipher, legacy)  # type: ignore[return-value]
        return cipher
    except Exception as e:
        logger.error(f"Failed to derive agent key: {e}")
        return None


# =============================================================================
# Purpose-Specific Encryption
# =============================================================================

def get_agent_key(agent_did: str, purpose: str) -> bytes:
    """
    Derive a 32-byte key for an agent and purpose.

    Args:
        agent_did: Agent's DID (required, cannot be empty)
        purpose: One of "conversations", "service-keys", "wallet", "backup"

    Returns:
        32-byte derived key

    Raises:
        MasterKeyNotConfiguredError: If KESTREL_DATA_KEY not set
        InvalidPurposeError: If purpose not in VALID_PURPOSES
        ValueError: If agent_did is empty
    """
    if not agent_did:
        raise ValueError("agent_did is required for encryption")

    if purpose not in VALID_PURPOSES:
        raise InvalidPurposeError(
            f"Invalid purpose '{purpose}'. Must be one of: {', '.join(sorted(VALID_PURPOSES))}"
        )

    return _derive_purpose_key(_get_master_key(), agent_did, purpose)


def _derive_purpose_key(master_key: bytes, agent_did: str, purpose: str) -> bytes:
    # First HKDF: derive agent master key
    hkdf_agent = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=agent_did.encode("utf-8"),
        info=b"kestrel-agent-master-v1",
    )
    agent_master_key = hkdf_agent.derive(master_key)

    # Second HKDF: derive purpose-specific key
    info = f"kestrel-{purpose}-v1".encode("utf-8")
    hkdf_purpose = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=None,
        info=info,
    )
    return hkdf_purpose.derive(agent_master_key)


def _get_legacy_agent_key(agent_did: str, purpose: str) -> bytes:
    """Derive the pre-#26 purpose key for decrypt-only fallback."""
    return _derive_purpose_key(_get_legacy_master_key(), agent_did, purpose)


def encrypt(agent_did: str, purpose: str, plaintext: bytes) -> bytes:
    """Encrypt bytes for an agent with purpose-specific key.

    Always emits v2 (``KSAv2:``) tokens. Existing Fernet-encrypted data
    keeps decrypting via the AEADCipher legacy-read path with the same
    derived key.
    """
    key = get_agent_key(agent_did, purpose)
    return AEADCipher(key).encrypt(plaintext)


def decrypt(agent_did: str, purpose: str, ciphertext: bytes) -> bytes:
    """Decrypt bytes for an agent with purpose-specific key.

    Accepts both v2 and legacy Fernet ciphertext.
    """
    key = get_agent_key(agent_did, purpose)
    try:
        return AEADCipher(key).decrypt(ciphertext)
    except DecryptionError as primary_error:
        try:
            legacy_key = _get_legacy_agent_key(agent_did, purpose)
        except Exception:
            raise primary_error
        if legacy_key == key:
            raise primary_error
        try:
            return AEADCipher(legacy_key).decrypt(ciphertext)
        except DecryptionError:
            raise primary_error
    except Exception as e:
        raise DecryptionError(f"Decryption failed: {e}") from e


def encrypt_string(agent_did: str, purpose: str, plaintext: str) -> bytes:
    """Encrypt a string for an agent."""
    return encrypt(agent_did, purpose, plaintext.encode("utf-8"))


def decrypt_string(agent_did: str, purpose: str, ciphertext: bytes) -> str:
    """Decrypt bytes to a string for an agent."""
    return decrypt(agent_did, purpose, ciphertext).decode("utf-8")


# =============================================================================
# Cipher-instance-based helpers
# =============================================================================
#
# Names retain the ``_fernet`` suffix for caller compatibility, but the
# parameter is an ``AEADCipher`` (drop-in for ``Fernet``). On encrypt we
# always emit v2; on decrypt we accept both v2 and legacy Fernet.

def encrypt_bytes(content: bytes, cipher: Optional[AEADCipher]) -> Tuple[bytes, bool]:
    """Encrypt bytes content if a cipher is available."""
    if cipher is None:
        return content, False
    return cipher.encrypt(content), True


def decrypt_bytes(content: bytes, cipher: Optional[AEADCipher], metadata: Optional[Dict[str, Any]] = None) -> bytes:
    """Decrypt bytes content if it was encrypted."""
    if cipher is None:
        if metadata and metadata.get("enc"):
            raise DecryptionError("No decryption key available but content is marked as encrypted")
        return content

    if metadata and metadata.get("enc"):
        try:
            return cipher.decrypt(content)
        except DecryptionError:
            raise
        except Exception as e:
            raise DecryptionError(f"Decryption failed: {e}") from e

    return content


def encrypt_string_fernet(content: str, cipher: Optional[AEADCipher]) -> Tuple[str, bool]:
    """Encrypt string content if a cipher is available.

    Name retained for legacy callers; parameter is now an ``AEADCipher``.

    Note: this helper does not expose Associated Data (AAD). Tokens written
    elsewhere with AAD bound will fail to decrypt through ``decrypt_string_fernet``
    (the AEADCipher will report a "wrong key, AAD, or tampering" diagnostic).
    Callers that need AAD-bound encryption should use ``AEADCipher.encrypt`` /
    ``AEADCipher.decrypt`` directly.
    """
    if cipher is None:
        return content, False
    encrypted = cipher.encrypt(content.encode('utf-8')).decode('utf-8')
    return encrypted, True


def decrypt_string_fernet(content: str, metadata: Optional[Dict[str, Any]], cipher: Optional[AEADCipher]) -> str:
    """Decrypt string content if it was encrypted.

    Name retained for legacy callers; parameter is now an ``AEADCipher``.
    Accepts both v2 (``KSAv2:``) and legacy Fernet ciphertext.

    Note: AAD is not exposed by this helper. A v2 token written with AAD
    elsewhere will fail to decrypt through this path; use ``AEADCipher.decrypt``
    directly if you need AAD-bound decryption.
    """
    if cipher is None:
        if metadata and metadata.get("enc"):
            raise DecryptionError("No decryption key available but content is marked as encrypted")
        return content

    if metadata and metadata.get("enc"):
        try:
            return cipher.decrypt(content.encode('utf-8')).decode('utf-8')
        except DecryptionError:
            raise
        except Exception as e:
            raise DecryptionError(f"Decryption failed: {e}") from e

    return content


def remove_enc_flag(metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Remove the internal 'enc' flag from metadata for external use."""
    if not metadata:
        return None
    cleaned = {k: v for k, v in metadata.items() if k != 'enc'}
    return cleaned if cleaned else None
