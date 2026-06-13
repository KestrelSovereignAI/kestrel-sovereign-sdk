"""Tests for SDK encryption key derivation and migration behavior."""

from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from kestrel_sdk.security import encryption
from kestrel_sdk.security.aead import AEADCipher


def _write_salt(path, salt: bytes) -> None:
    path.write_text(base64.urlsafe_b64encode(salt).decode("ascii"), encoding="utf-8")


def _old_global_key(key: str, fernet_shaped: bool) -> bytes | str:
    if fernet_shaped:
        return key
    return hashlib.sha256(key.encode("utf-8")).digest()


def _old_purpose_key(key: str, agent_did: str, purpose: str) -> bytes:
    master = hashlib.sha256(key.encode("utf-8")).digest()
    return encryption._derive_purpose_key(master, agent_did, purpose)


def _old_agent_cipher_key(key: str, agent_id: str, fernet_shaped: bool) -> bytes:
    if fernet_shaped:
        master = key.encode("ascii")
    else:
        master = base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest())
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=encryption.KEY_SIZE,
        salt=agent_id.encode("utf-8"),
        info=b"kestrel-agent-v1",
    )
    return hkdf.derive(master)


@pytest.fixture(params=["fernet", "passphrase_salt", "passphrase_no_salt"])
def key_shape(request, monkeypatch, tmp_path):
    monkeypatch.delenv(encryption.ENV_VAR_NAME, raising=False)
    monkeypatch.delenv("KESTREL_DATA_KEY_FILE", raising=False)
    monkeypatch.delenv(encryption.SALT_VALUE_ENV_VAR_NAME, raising=False)
    monkeypatch.delenv(encryption.SALT_ENV_VAR_NAME, raising=False)

    if request.param == "fernet":
        key = AEADCipher.generate_key().decode("ascii")
        monkeypatch.setenv(encryption.ENV_VAR_NAME, key)
        return {"key": key, "fernet_shaped": True, "salted": False}

    key = f"legacy migration passphrase {request.param}"
    monkeypatch.setenv(encryption.ENV_VAR_NAME, key)
    if request.param == "passphrase_salt":
        salt_file = tmp_path / "kestrel_data_key.salt"
        _write_salt(salt_file, b"\x42" * encryption.PASSPHRASE_SALT_SIZE)
        monkeypatch.setenv(encryption.SALT_ENV_VAR_NAME, str(salt_file))
        return {"key": key, "fernet_shaped": False, "salted": True}

    return {"key": key, "fernet_shaped": False, "salted": False}


@pytest.fixture(autouse=True)
def clear_master_key_cache():
    encryption._PASSPHRASE_MASTER_KEY_CACHE.clear()
    encryption._PASSPHRASE_NO_SALT_WARNING_EMITTED = False
    yield
    encryption._PASSPHRASE_MASTER_KEY_CACHE.clear()
    encryption._PASSPHRASE_NO_SALT_WARNING_EMITTED = False


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


def test_env_passphrase_without_salt_warns_once_and_uses_legacy(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv(encryption.ENV_VAR_NAME, "bare passphrase")
    monkeypatch.delenv("KESTREL_DATA_KEY_FILE", raising=False)
    monkeypatch.delenv(encryption.SALT_VALUE_ENV_VAR_NAME, raising=False)
    monkeypatch.delenv(encryption.SALT_ENV_VAR_NAME, raising=False)
    salt_file = tmp_path / "should-not-exist.salt"

    first = encryption.get_master_key_bytes()
    second = encryption.get_master_key_bytes()

    legacy = base64.urlsafe_b64encode(hashlib.sha256(b"bare passphrase").digest())
    assert first == legacy
    assert second == legacy
    assert not salt_file.exists()
    assert caplog.text.count("legacy unsalted SHA-256 mode") == 1


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


def test_key_file_passphrase_salt_write_failure_falls_back_to_legacy(monkeypatch, caplog, tmp_path):
    key_file = tmp_path / "kestrel_data_key"
    passphrase = "read only secret mount passphrase"
    key_file.write_text(passphrase, encoding="utf-8")
    real_write_text = encryption.Path.write_text

    def fail_salt_write(self, *args, **kwargs):
        if str(self).endswith(".salt"):
            raise OSError("read-only secret mount")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setenv("KESTREL_DATA_KEY_FILE", str(key_file))
    monkeypatch.delenv(encryption.ENV_VAR_NAME, raising=False)
    monkeypatch.delenv(encryption.SALT_VALUE_ENV_VAR_NAME, raising=False)
    monkeypatch.delenv(encryption.SALT_ENV_VAR_NAME, raising=False)
    monkeypatch.setattr(encryption.Path, "write_text", fail_salt_write)

    first = encryption.get_master_key_bytes()
    second = encryption.get_master_key_bytes()
    legacy = base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode("utf-8")).digest())

    assert first == legacy
    assert second == legacy
    assert not key_file.with_name(key_file.name + ".salt").exists()
    assert caplog.text.count("legacy unsalted SHA-256 mode") == 1

    agent_did = "did:key:z6MkReadOnlySecret"
    purpose = "conversations"
    ciphertext = encryption.encrypt(agent_did, purpose, b"legacy write")

    assert encryption.decrypt(agent_did, purpose, ciphertext) == b"legacy write"
    assert AEADCipher(_old_purpose_key(passphrase, agent_did, purpose)).decrypt(ciphertext) == b"legacy write"


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


def test_global_decrypt_reads_old_ciphertext_for_all_key_shapes(key_shape):
    legacy_key = _old_global_key(key_shape["key"], key_shape["fernet_shaped"])
    legacy_ciphertext = AEADCipher(legacy_key).encrypt(b"legacy global plaintext")

    cipher = encryption.get_fernet()
    assert cipher is not None
    assert cipher.decrypt(legacy_ciphertext) == b"legacy global plaintext"


def test_purpose_decrypt_reads_old_ciphertext_for_all_key_shapes(key_shape):
    agent_did = "did:key:z6MkPurposeLegacy"
    purpose = "conversations"
    legacy_key = _old_purpose_key(key_shape["key"], agent_did, purpose)
    legacy_ciphertext = AEADCipher(legacy_key).encrypt(b"legacy purpose plaintext")

    assert encryption.decrypt(agent_did, purpose, legacy_ciphertext) == b"legacy purpose plaintext"


def test_per_agent_decrypt_reads_old_ciphertext_for_all_key_shapes(key_shape):
    agent_did = "did:key:z6MkAgentLegacy"
    legacy_key = _old_agent_cipher_key(
        key_shape["key"],
        agent_did,
        key_shape["fernet_shaped"],
    )
    legacy_ciphertext = AEADCipher(legacy_key).encrypt(b"legacy per-agent plaintext")

    cipher = encryption.get_agent_fernet(agent_did)
    assert cipher is not None
    assert cipher.decrypt(legacy_ciphertext) == b"legacy per-agent plaintext"


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


def test_passphrase_without_salt_purpose_round_trips_as_legacy(monkeypatch):
    passphrase = "legacy-only deployment passphrase"
    monkeypatch.setenv(encryption.ENV_VAR_NAME, passphrase)
    monkeypatch.delenv("KESTREL_DATA_KEY_FILE", raising=False)
    monkeypatch.delenv(encryption.SALT_VALUE_ENV_VAR_NAME, raising=False)
    monkeypatch.delenv(encryption.SALT_ENV_VAR_NAME, raising=False)

    agent_did = "did:key:z6MkNoSalt"
    purpose = "conversations"

    ciphertext = encryption.encrypt(agent_did, purpose, b"legacy write")
    legacy_key = _old_purpose_key(passphrase, agent_did, purpose)

    assert AEADCipher(legacy_key).decrypt(ciphertext) == b"legacy write"


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


def test_fernet_key_agent_key_and_agent_fernet_are_deterministic(monkeypatch, tmp_path):
    """Verify both Fernet-key derivation APIs stay deterministic."""
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

    assert encryption.decrypt(agent_did, purpose, ciphertext_purpose) == plaintext

    agent_ciphertext = agent_cipher.encrypt(plaintext)
    assert agent_cipher.decrypt(agent_ciphertext) == plaintext

    key1 = encryption.get_agent_key(agent_did, purpose)
    key2 = encryption.get_agent_key(agent_did, purpose)
    assert key1 == key2  # Should be deterministic from same master
