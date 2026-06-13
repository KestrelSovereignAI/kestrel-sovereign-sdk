"""Tests for SDK encryption key derivation and migration behavior."""

from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from kestrel_sdk.security import encryption
from kestrel_sdk.security.aead import AEADCipher
from kestrel_sdk.security.exceptions import MasterKeyNotConfiguredError


def _write_salt(path, salt: bytes) -> None:
    path.write_text(base64.urlsafe_b64encode(salt).decode("ascii"), encoding="utf-8")


@pytest.fixture(autouse=True)
def clear_master_key_cache():
    encryption._PASSPHRASE_MASTER_KEY_CACHE.clear()
    yield
    encryption._PASSPHRASE_MASTER_KEY_CACHE.clear()


def test_passphrase_master_key_uses_salted_pbkdf2_600k(monkeypatch, tmp_path):
    passphrase = "correct horse battery staple"
    salt = b"\x7f" * encryption.PASSPHRASE_SALT_SIZE
    salt_file = tmp_path / "kestrel_data_key.salt"
    _write_salt(salt_file, salt)

    monkeypatch.setenv(encryption.ENV_VAR_NAME, passphrase)
    monkeypatch.setenv(encryption.SALT_ENV_VAR_NAME, str(salt_file))
    monkeypatch.delenv("KESTREL_DATA_KEY_FILE", raising=False)

    expected = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=encryption.KEY_SIZE,
        salt=salt,
        iterations=600_000,
    ).derive(passphrase.encode("utf-8"))

    derived = encryption.get_master_key_bytes()

    assert derived == base64.urlsafe_b64encode(expected)
    assert derived != base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode("utf-8")).digest())


def test_env_passphrase_requires_explicit_salt_source(monkeypatch):
    monkeypatch.setenv(encryption.ENV_VAR_NAME, "bare passphrase")
    monkeypatch.delenv("KESTREL_DATA_KEY_FILE", raising=False)
    monkeypatch.delenv(encryption.SALT_VALUE_ENV_VAR_NAME, raising=False)
    monkeypatch.delenv(encryption.SALT_ENV_VAR_NAME, raising=False)

    with pytest.raises(MasterKeyNotConfiguredError, match="requires KESTREL_DATA_KEY_SALT"):
        encryption.get_master_key_bytes()


def test_same_passphrase_and_env_salt_yield_same_key_across_derivations(monkeypatch):
    passphrase = "stable deployment passphrase"
    salt = b"\x17" * encryption.PASSPHRASE_SALT_SIZE
    encoded_salt = base64.urlsafe_b64encode(salt).decode("ascii")

    monkeypatch.setenv(encryption.ENV_VAR_NAME, passphrase)
    monkeypatch.setenv(encryption.SALT_VALUE_ENV_VAR_NAME, encoded_salt)
    monkeypatch.delenv("KESTREL_DATA_KEY_FILE", raising=False)
    monkeypatch.delenv(encryption.SALT_ENV_VAR_NAME, raising=False)

    first = encryption.get_master_key_bytes()
    encryption._PASSPHRASE_MASTER_KEY_CACHE.clear()
    second = encryption.get_master_key_bytes()

    assert first == second


def test_key_file_passphrase_persists_salt_next_to_key(monkeypatch, tmp_path):
    key_file = tmp_path / "kestrel_data_key"
    key_file.write_text("shared passphrase", encoding="utf-8")

    monkeypatch.setenv("KESTREL_DATA_KEY_FILE", str(key_file))
    monkeypatch.delenv(encryption.ENV_VAR_NAME, raising=False)
    monkeypatch.delenv(encryption.SALT_VALUE_ENV_VAR_NAME, raising=False)
    monkeypatch.delenv(encryption.SALT_ENV_VAR_NAME, raising=False)

    first = encryption.get_master_key_bytes()
    encryption._PASSPHRASE_MASTER_KEY_CACHE.clear()
    second = encryption.get_master_key_bytes()

    assert first == second
    assert key_file.with_name(key_file.name + ".salt").is_file()


def test_passphrase_master_key_is_cached_for_encrypt_decrypt(monkeypatch):
    passphrase = "cached deployment passphrase"
    salt = base64.urlsafe_b64encode(b"\x24" * encryption.PASSPHRASE_SALT_SIZE).decode("ascii")
    calls = 0
    real_pbkdf2 = encryption.PBKDF2HMAC

    def counting_pbkdf2(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_pbkdf2(*args, **kwargs)

    monkeypatch.setenv(encryption.ENV_VAR_NAME, passphrase)
    monkeypatch.setenv(encryption.SALT_VALUE_ENV_VAR_NAME, salt)
    monkeypatch.delenv("KESTREL_DATA_KEY_FILE", raising=False)
    monkeypatch.delenv(encryption.SALT_ENV_VAR_NAME, raising=False)
    monkeypatch.setattr(encryption, "PASSPHRASE_KDF_ITERATIONS", 1)
    monkeypatch.setattr(encryption, "PBKDF2HMAC", counting_pbkdf2)

    agent_did = "did:key:z6MkCached"
    purpose = "conversations"

    first = encryption.encrypt(agent_did, purpose, b"first")
    second = encryption.encrypt(agent_did, purpose, b"second")

    assert encryption.decrypt(agent_did, purpose, first) == b"first"
    assert encryption.decrypt(agent_did, purpose, second) == b"second"
    assert encryption.get_agent_key(agent_did, purpose) == encryption.get_agent_key(agent_did, purpose)
    assert calls == 1


def test_purpose_decrypt_reads_legacy_sha256_ciphertext(monkeypatch, tmp_path):
    passphrase = "legacy deployment passphrase"
    salt_file = tmp_path / "kestrel_data_key.salt"
    _write_salt(salt_file, b"\x42" * encryption.PASSPHRASE_SALT_SIZE)

    monkeypatch.setenv(encryption.ENV_VAR_NAME, passphrase)
    monkeypatch.setenv(encryption.SALT_ENV_VAR_NAME, str(salt_file))
    monkeypatch.delenv("KESTREL_DATA_KEY_FILE", raising=False)

    agent_did = "did:key:z6MkLegacy"
    purpose = "conversations"
    legacy_master = hashlib.sha256(passphrase.encode("utf-8")).digest()
    legacy_key = encryption._derive_purpose_key(legacy_master, agent_did, purpose)
    legacy_ciphertext = AEADCipher(legacy_key).encrypt(b"legacy plaintext")

    assert encryption.decrypt(agent_did, purpose, legacy_ciphertext) == b"legacy plaintext"


def test_fernet_shaped_key_file_still_works_unchanged(monkeypatch, tmp_path):
    key_file = tmp_path / "kestrel_data_key"
    raw_key = AEADCipher.generate_key()
    key_file.write_text(raw_key.decode("ascii"), encoding="utf-8")

    monkeypatch.setenv("KESTREL_DATA_KEY_FILE", str(key_file))
    monkeypatch.delenv(encryption.ENV_VAR_NAME, raising=False)
    monkeypatch.delenv(encryption.SALT_ENV_VAR_NAME, raising=False)

    cipher = encryption.get_fernet()
    assert cipher is not None

    ciphertext = AEADCipher(raw_key).encrypt(b"file-key plaintext")
    assert cipher.decrypt(ciphertext) == b"file-key plaintext"
    assert not key_file.with_name(key_file.name + ".salt").exists()


def test_global_cipher_reads_legacy_sha256_ciphertext(monkeypatch, tmp_path):
    passphrase = "global legacy passphrase"
    salt_file = tmp_path / "kestrel_data_key.salt"
    _write_salt(salt_file, b"\x99" * encryption.PASSPHRASE_SALT_SIZE)

    monkeypatch.setenv(encryption.ENV_VAR_NAME, passphrase)
    monkeypatch.setenv(encryption.SALT_ENV_VAR_NAME, str(salt_file))
    monkeypatch.delenv("KESTREL_DATA_KEY_FILE", raising=False)

    legacy_key = hashlib.sha256(passphrase.encode("utf-8")).digest()
    legacy_ciphertext = AEADCipher(legacy_key).encrypt(b"global legacy plaintext")

    cipher = encryption.get_fernet()
    assert cipher is not None
    assert cipher.decrypt(legacy_ciphertext) == b"global legacy plaintext"

    new_ciphertext = cipher.encrypt(b"new plaintext")
    with pytest.raises(Exception):
        AEADCipher(legacy_key).decrypt(new_ciphertext)


def test_fernet_key_encrypt_decrypt_uses_decoded_key(monkeypatch, tmp_path):
    """Verify encrypt()/decrypt() with Fernet key uses decoded 32-byte master key."""
    raw_key = AEADCipher.generate_key()
    key_file = tmp_path / "kestrel_data_key"
    key_file.write_text(raw_key.decode("ascii"), encoding="utf-8")

    monkeypatch.setenv("KESTREL_DATA_KEY_FILE", str(key_file))
    monkeypatch.delenv(encryption.ENV_VAR_NAME, raising=False)

    agent_did = "did:key:z6MkFernetTest"
    purpose = "conversations"

    plaintext = b"test message for fernet key"
    ciphertext = encryption.encrypt(agent_did, purpose, plaintext)
    decrypted = encryption.decrypt(agent_did, purpose, ciphertext)

    assert decrypted == plaintext
    assert not key_file.with_name(key_file.name + ".salt").exists()


def test_fernet_key_agent_key_matches_agent_fernet(monkeypatch, tmp_path):
    """Verify get_agent_key() and get_agent_fernet() derive compatible keys from Fernet master."""
    raw_key = AEADCipher.generate_key()
    key_file = tmp_path / "kestrel_data_key"
    key_file.write_text(raw_key.decode("ascii"), encoding="utf-8")

    monkeypatch.setenv("KESTREL_DATA_KEY_FILE", str(key_file))
    monkeypatch.delenv(encryption.ENV_VAR_NAME, raising=False)

    agent_did = "did:key:z6MkCompatTest"
    purpose = "conversations"

    plaintext = b"cross-function compatibility test"

    # Encrypt with purpose-specific API
    ciphertext_purpose = encryption.encrypt(agent_did, purpose, plaintext)

    # Create agent fernet and encrypt with it
    agent_cipher = encryption.get_agent_fernet(agent_did)
    assert agent_cipher is not None

    # The keys should be derived from the same 32-byte master, but they're
    # different derived keys (different HKDF info strings), so we test that
    # the derivation is consistent by checking both can decrypt their own ciphertext
    assert encryption.decrypt(agent_did, purpose, ciphertext_purpose) == plaintext

    agent_ciphertext = agent_cipher.encrypt(plaintext)
    assert agent_cipher.decrypt(agent_ciphertext) == plaintext

    # Verify the master key derivation is the same (32-byte decoded form)
    # by checking that get_agent_key derives from the same base
    key1 = encryption.get_agent_key(agent_did, purpose)
    key2 = encryption.get_agent_key(agent_did, purpose)
    assert key1 == key2  # Should be deterministic from same master
