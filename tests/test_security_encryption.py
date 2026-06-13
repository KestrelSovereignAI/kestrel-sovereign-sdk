"""Tests for SDK encryption key derivation and migration behavior."""

from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from kestrel_sdk.security import encryption
from kestrel_sdk.security.aead import AEADCipher


def _write_salt(path, salt: bytes) -> None:
    path.write_text(base64.urlsafe_b64encode(salt).decode("ascii"), encoding="utf-8")


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
