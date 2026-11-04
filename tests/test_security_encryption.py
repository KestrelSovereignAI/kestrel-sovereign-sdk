"""Tests for SDK encryption key derivation and migration behavior."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import multiprocessing
import os
import re
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from kestrel_sdk.security import encryption
from kestrel_sdk.security.aead import AEADCipher
from kestrel_sdk.security.exceptions import MasterKeyNotConfiguredError

# Spawned children are fresh interpreters, like independent hosts or a restart.
_SPAWN = multiprocessing.get_context("spawn")
_CHILD_TIMEOUT_SECONDS = 60
_INTERRUPTED_EXIT_CODE = 17
_PUBLISHER_THREAD = "salt-publisher"
_WRITE_REFUSAL = re.compile(
    r"Refusing to encrypt new data with the legacy unsalted key.*"
    r"KESTREL_DATA_KEY_SALT .*KESTREL_DATA_KEY_SALT_FILE"
)

requires_posix_permissions = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs POSIX permission bits enforced for a non-root user",
)


def _write_salt(path, salt: bytes) -> None:
    path.write_text(base64.urlsafe_b64encode(salt).decode("ascii"), encoding="utf-8")


def _salt_path(key_file: Path) -> Path:
    return key_file.with_name(key_file.name + ".salt")


def _salted_master_key_bytes(passphrase: str, salt: bytes) -> bytes:
    derived = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=encryption.KEY_SIZE,
        salt=salt,
        iterations=600_000,
    ).derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(derived)


def _use_only_key_file(monkeypatch, key_file: Path) -> None:
    monkeypatch.setenv(encryption.KEY_FILE_ENV_VAR_NAME, str(key_file))
    monkeypatch.delenv(encryption.ENV_VAR_NAME, raising=False)
    monkeypatch.delenv(encryption.SALT_VALUE_ENV_VAR_NAME, raising=False)
    monkeypatch.delenv(encryption.SALT_ENV_VAR_NAME, raising=False)


def _child_uses_only_key_file(key_file: str) -> None:
    for name in (
        encryption.ENV_VAR_NAME,
        encryption.SALT_VALUE_ENV_VAR_NAME,
        encryption.SALT_ENV_VAR_NAME,
    ):
        os.environ.pop(name, None)
    os.environ[encryption.KEY_FILE_ENV_VAR_NAME] = key_file


def _contend_for_new_salt(key_file: str, barrier, agent_did: str, result_file: str) -> None:
    """Child: race another process to create the salt within its first write."""
    _child_uses_only_key_file(key_file)
    publish = encryption._publish_salt_file

    def publish_after_every_contender_saw_no_salt(salt_path, salt):
        barrier.wait(timeout=_CHILD_TIMEOUT_SECONDS)
        publish(salt_path, salt)

    encryption._publish_salt_file = publish_after_every_contender_saw_no_salt
    ciphertext = encryption.encrypt(agent_did, "conversations", f"written by {agent_did}".encode())
    Path(result_file).write_bytes(ciphertext)


def _decrypt_after_restart(key_file: str, ciphertexts: list, result_file: str) -> None:
    """Child: a fresh start that only has the key file and its durable salt."""
    _child_uses_only_key_file(key_file)
    plaintexts = [
        encryption.decrypt(agent_did, "conversations", ciphertext.encode("ascii")).decode("utf-8")
        for agent_did, ciphertext in ciphertexts
    ]
    Path(result_file).write_text(json.dumps(plaintexts), encoding="utf-8")


def _crash_before_publishing_salt(key_file: str) -> None:
    """Child: die after the temp salt is durable but before it is published."""
    _child_uses_only_key_file(key_file)

    def crash(src, dst):
        os._exit(_INTERRUPTED_EXIT_CODE)

    os.link = crash
    encryption.get_master_key_bytes()


def _start_child(target, *args):
    process = _SPAWN.Process(target=target, args=args)
    process.start()
    return process


def _join_child(process, expected_exitcode: int = 0) -> None:
    process.join(_CHILD_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("child process did not finish")
    assert process.exitcode == expected_exitcode


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
    encryption._DURABLE_KEY_FILE_SALTS.clear()
    encryption._PASSPHRASE_NO_SALT_WARNING_EMITTED = False
    yield
    encryption._PASSPHRASE_MASTER_KEY_CACHE.clear()
    encryption._DURABLE_KEY_FILE_SALTS.clear()
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


def test_key_file_salt_is_published_privately_without_temp_files(monkeypatch, tmp_path):
    key_file = tmp_path / "kestrel_data_key"
    passphrase = "private salt passphrase"
    key_file.write_text(passphrase, encoding="utf-8")
    _use_only_key_file(monkeypatch, key_file)

    master = encryption.get_master_key_bytes()

    salt_path = _salt_path(key_file)
    salt = base64.urlsafe_b64decode(salt_path.read_bytes())
    assert len(salt) == encryption.PASSPHRASE_SALT_SIZE
    assert master == _salted_master_key_bytes(passphrase, salt)
    assert sorted(tmp_path.iterdir()) == [key_file, salt_path]
    if os.name == "posix":
        assert stat.S_IMODE(salt_path.stat().st_mode) == 0o600


def test_concurrent_first_starts_converge_on_one_durable_salt(tmp_path):
    key_file = tmp_path / "kestrel_data_key"
    passphrase = "shared passphrase for two first starts"
    key_file.write_text(passphrase, encoding="utf-8")
    barrier = _SPAWN.Barrier(2)
    contenders = [
        ("did:key:z6MkFirstStartA", tmp_path / "a.ciphertext"),
        ("did:key:z6MkFirstStartB", tmp_path / "b.ciphertext"),
    ]

    processes = [
        _start_child(_contend_for_new_salt, str(key_file), barrier, agent_did, str(result_file))
        for agent_did, result_file in contenders
    ]
    for process in processes:
        _join_child(process)
    ciphertexts = [(agent_did, result_file.read_bytes()) for agent_did, result_file in contenders]

    # Each first write used the key derived from the one durable salt.
    salt_path = _salt_path(key_file)
    durable_master = base64.urlsafe_b64decode(
        _salted_master_key_bytes(passphrase, base64.urlsafe_b64decode(salt_path.read_bytes()))
    )
    for agent_did, ciphertext in ciphertexts:
        purpose_key = encryption._derive_purpose_key(durable_master, agent_did, "conversations")
        assert AEADCipher(purpose_key).decrypt(ciphertext) == f"written by {agent_did}".encode()

    restart_file = tmp_path / "restart.json"
    _join_child(
        _start_child(
            _decrypt_after_restart,
            str(key_file),
            [(agent_did, ciphertext.decode("ascii")) for agent_did, ciphertext in ciphertexts],
            str(restart_file),
        )
    )

    assert json.loads(restart_file.read_text(encoding="utf-8")) == [
        f"written by {agent_did}" for agent_did, _ in contenders
    ]
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [key_file.name, salt_path.name, restart_file.name, *(result.name for _, result in contenders)]
    )


def test_salt_published_after_existence_check_is_adopted_not_replaced(monkeypatch, tmp_path):
    key_file = tmp_path / "kestrel_data_key"
    passphrase = "passphrase that loses the salt race"
    key_file.write_text(passphrase, encoding="utf-8")
    winner_salt = b"\x5a" * encryption.PASSPHRASE_SALT_SIZE
    publish = encryption._publish_salt_file

    def lose_race(salt_path, salt):
        publish(salt_path, winner_salt)
        publish(salt_path, salt)

    real_fsync_directory = encryption._fsync_directory
    synced = []

    def record_sync(directory):
        synced.append(directory)
        real_fsync_directory(directory)

    _use_only_key_file(monkeypatch, key_file)
    monkeypatch.setattr(encryption, "_publish_salt_file", lose_race)
    monkeypatch.setattr(encryption, "_fsync_directory", record_sync)

    assert encryption.get_master_key_bytes() == _salted_master_key_bytes(passphrase, winner_salt)
    salt_path = _salt_path(key_file)
    assert base64.urlsafe_b64decode(salt_path.read_bytes()) == winner_salt
    assert sorted(tmp_path.iterdir()) == [key_file, salt_path]
    # The loser makes the winner's entry durable itself before using it.
    assert synced == [tmp_path]


def test_salt_adopted_before_its_publisher_syncs_is_synced_before_any_write(monkeypatch, tmp_path):
    key_file = tmp_path / "kestrel_data_key"
    key_file.write_text("passphrase adopted before its publisher synced", encoding="utf-8")
    _use_only_key_file(monkeypatch, key_file)
    purpose = "conversations"
    publisher_did = "did:key:z6MkSaltPublisher"
    adopter_did = "did:key:z6MkSaltAdopter"
    real_fsync_directory = encryption._fsync_directory
    publisher_linked = threading.Event()
    release_publisher = threading.Event()
    adopter_sync_fails = True
    adopter_syncs = []

    def fsync_directory(directory):
        if threading.current_thread().name.startswith(_PUBLISHER_THREAD):
            # Hold the publisher after os.link, immediately before its sync.
            publisher_linked.set()
            assert release_publisher.wait(_CHILD_TIMEOUT_SECONDS)
        else:
            adopter_syncs.append(directory)
            if adopter_sync_fails:
                raise OSError(errno.EIO, os.strerror(errno.EIO), str(directory))
        real_fsync_directory(directory)

    monkeypatch.setattr(encryption, "_fsync_directory", fsync_directory)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix=_PUBLISHER_THREAD) as executor:
        publisher = executor.submit(encryption.encrypt, publisher_did, purpose, b"publisher write")
        try:
            assert publisher_linked.wait(_CHILD_TIMEOUT_SECONDS)
            assert _salt_path(key_file).is_file()

            # Without its own sync, a crash could still lose the salt this write uses.
            with pytest.raises(MasterKeyNotConfiguredError, match="durable"):
                encryption.encrypt(adopter_did, purpose, b"adopter write")
            adopter_sync_fails = False
            adopter_ciphertext = encryption.encrypt(adopter_did, purpose, b"adopter write")
        finally:
            release_publisher.set()
        publisher_ciphertext = publisher.result(timeout=_CHILD_TIMEOUT_SECONDS)

    assert adopter_syncs == [tmp_path, tmp_path]
    assert encryption.decrypt(adopter_did, purpose, adopter_ciphertext) == b"adopter write"
    assert encryption.decrypt(publisher_did, purpose, publisher_ciphertext) == b"publisher write"


def test_established_key_file_salt_is_synced_once_per_process_before_use(monkeypatch, tmp_path):
    key_file = tmp_path / "kestrel_data_key"
    passphrase = "established salt synced before first use"
    key_file.write_text(passphrase, encoding="utf-8")
    salt = b"\x44" * encryption.PASSPHRASE_SALT_SIZE
    _write_salt(_salt_path(key_file), salt)
    real_fsync_directory = encryption._fsync_directory
    synced = []
    sync_fails = True

    def fsync_directory(directory):
        synced.append(directory)
        if sync_fails:
            raise OSError(errno.EIO, os.strerror(errno.EIO), str(directory))
        real_fsync_directory(directory)

    _use_only_key_file(monkeypatch, key_file)
    monkeypatch.setattr(encryption, "_fsync_directory", fsync_directory)
    agent_did = "did:key:z6MkSyncedOnce"

    with pytest.raises(MasterKeyNotConfiguredError, match=re.escape(str(_salt_path(key_file)))):
        encryption.encrypt(agent_did, "conversations", b"must not be written")
    sync_fails = False
    ciphertext = encryption.encrypt(agent_did, "conversations", b"salted write")
    assert encryption.decrypt(agent_did, "conversations", ciphertext) == b"salted write"
    assert encryption.get_master_key_bytes() == _salted_master_key_bytes(passphrase, salt)

    assert synced == [tmp_path, tmp_path]


@pytest.mark.parametrize("fernet_shaped", [True, False], ids=["fernet", "passphrase"])
def test_each_operation_derives_every_key_from_one_key_file_read(monkeypatch, tmp_path, fernet_shaped):
    key_file = tmp_path / "kestrel_data_key"
    if fernet_shaped:
        key = AEADCipher.generate_key().decode("ascii")
    else:
        key = "passphrase read once per operation"
    read_required_key_file = encryption._read_required_key_file

    def read_then_vanish(path):
        # A second read within the same operation would find no key file.
        try:
            return read_required_key_file(path)
        finally:
            path.unlink(missing_ok=True)

    def with_key_file(operation):
        key_file.write_text(key, encoding="utf-8")
        return operation()

    _use_only_key_file(monkeypatch, key_file)
    monkeypatch.setattr(encryption, "_read_required_key_file", read_then_vanish)
    agent_did = "did:key:z6MkReadOnce"
    purpose = "conversations"
    legacy_agent = AEADCipher(_old_agent_cipher_key(key, agent_did, fernet_shaped)).encrypt(b"legacy agent")

    agent_cipher = with_key_file(lambda: encryption.get_agent_fernet(agent_did))
    assert agent_cipher is not None
    stored, encrypted = encryption.encrypt_bytes(b"agent write", agent_cipher)
    assert encrypted and stored != b"agent write"
    stored_text, encrypted = encryption.encrypt_string_fernet("agent write", agent_cipher)
    assert encrypted and stored_text != "agent write"
    assert agent_cipher.decrypt(stored) == b"agent write"
    assert agent_cipher.decrypt(legacy_agent) == b"legacy agent"

    global_cipher = with_key_file(encryption.get_fernet)
    assert global_cipher.decrypt(global_cipher.encrypt(b"global write")) == b"global write"

    ciphertext = with_key_file(lambda: encryption.encrypt(agent_did, purpose, b"purpose write"))
    assert with_key_file(lambda: encryption.decrypt(agent_did, purpose, ciphertext)) == b"purpose write"
    if not fernet_shaped:
        legacy_purpose = AEADCipher(_old_purpose_key(key, agent_did, purpose)).encrypt(b"legacy purpose")
        assert with_key_file(
            lambda: encryption.decrypt(agent_did, purpose, legacy_purpose)
        ) == b"legacy purpose"


def test_interrupted_salt_creation_does_not_block_next_start(monkeypatch, tmp_path):
    key_file = tmp_path / "kestrel_data_key"
    passphrase = "passphrase whose first start crashed"
    key_file.write_text(passphrase, encoding="utf-8")

    _join_child(
        _start_child(_crash_before_publishing_salt, str(key_file)),
        expected_exitcode=_INTERRUPTED_EXIT_CODE,
    )
    salt_path = _salt_path(key_file)
    leftovers = [path for path in tmp_path.iterdir() if path != key_file]
    assert not salt_path.exists()
    assert len(leftovers) == 1
    unpublished_salt = base64.urlsafe_b64decode(leftovers[0].read_bytes())

    _use_only_key_file(monkeypatch, key_file)
    master = encryption.get_master_key_bytes()

    salt = base64.urlsafe_b64decode(salt_path.read_bytes())
    assert salt != unpublished_salt
    assert master == _salted_master_key_bytes(passphrase, salt)
    agent_did = "did:key:z6MkAfterCrash"
    ciphertext = encryption.encrypt(agent_did, "conversations", b"after crash")
    assert encryption.decrypt(agent_did, "conversations", ciphertext) == b"after crash"


def _assert_writes_refused_and_legacy_reads(passphrase: str, key_file: Path) -> None:
    agent_did = "did:key:z6MkReadOnlySecret"
    purpose = "conversations"
    legacy_purpose = AEADCipher(_old_purpose_key(passphrase, agent_did, purpose)).encrypt(b"legacy purpose")
    legacy_global = AEADCipher(_old_global_key(passphrase, False)).encrypt(b"legacy global")
    legacy_agent = AEADCipher(_old_agent_cipher_key(passphrase, agent_did, False)).encrypt(b"legacy agent")

    global_cipher = encryption.get_fernet()
    agent_cipher = encryption.get_agent_fernet(agent_did)
    writes = (
        lambda: encryption.encrypt(agent_did, purpose, b"new write"),
        lambda: encryption.encrypt_string(agent_did, purpose, "new write"),
        lambda: encryption.get_agent_key(agent_did, purpose),
        encryption.get_master_key_bytes,
        lambda: global_cipher.encrypt(b"new write"),
        lambda: agent_cipher.encrypt(b"new write"),
        lambda: encryption.encrypt_bytes(b"new write", global_cipher),
    )
    for write in writes:
        with pytest.raises(MasterKeyNotConfiguredError, match=_WRITE_REFUSAL):
            write()

    assert encryption.decrypt(agent_did, purpose, legacy_purpose) == b"legacy purpose"
    assert global_cipher.decrypt(legacy_global) == b"legacy global"
    assert agent_cipher.decrypt(legacy_agent) == b"legacy agent"
    assert not _salt_path(key_file).exists()


def test_key_file_passphrase_salt_creation_failure_refuses_writes_and_reads_legacy(
    monkeypatch, caplog, tmp_path
):
    key_file = tmp_path / "kestrel_data_key"
    passphrase = "read only secret mount passphrase"
    key_file.write_text(passphrase, encoding="utf-8")

    def read_only_mount(salt_path, salt):
        raise OSError(errno.EROFS, os.strerror(errno.EROFS), str(salt_path))

    _use_only_key_file(monkeypatch, key_file)
    monkeypatch.setattr(encryption, "_publish_salt_file", read_only_mount)

    _assert_writes_refused_and_legacy_reads(passphrase, key_file)
    assert "legacy unsalted SHA-256 mode" not in caplog.text


@requires_posix_permissions
def test_key_file_passphrase_in_read_only_directory_refuses_writes_until_writable(
    monkeypatch, caplog, tmp_path
):
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    key_file = secret_dir / "kestrel_data_key"
    passphrase = "read only directory passphrase"
    key_file.write_text(passphrase, encoding="utf-8")
    _use_only_key_file(monkeypatch, key_file)

    secret_dir.chmod(0o500)
    try:
        _assert_writes_refused_and_legacy_reads(passphrase, key_file)
    finally:
        secret_dir.chmod(0o700)
    assert "legacy unsalted SHA-256 mode" not in caplog.text

    master = encryption.get_master_key_bytes()
    salt = base64.urlsafe_b64decode(_salt_path(key_file).read_bytes())
    assert master == _salted_master_key_bytes(passphrase, salt)


def test_established_key_file_salt_needs_no_writable_directory(monkeypatch, tmp_path):
    key_file = tmp_path / "kestrel_data_key"
    passphrase = "established salt passphrase"
    key_file.write_text(passphrase, encoding="utf-8")
    salt = b"\x33" * encryption.PASSPHRASE_SALT_SIZE
    _write_salt(_salt_path(key_file), salt)

    def must_not_publish(salt_path, salt):
        raise AssertionError("an established salt must never be replaced")

    _use_only_key_file(monkeypatch, key_file)
    monkeypatch.setattr(encryption, "_publish_salt_file", must_not_publish)

    assert encryption.get_master_key_bytes() == _salted_master_key_bytes(passphrase, salt)
    agent_did = "did:key:z6MkEstablished"
    ciphertext = encryption.encrypt(agent_did, "conversations", b"salted write")
    assert encryption.decrypt(agent_did, "conversations", ciphertext) == b"salted write"


def _break_key_file(key_file: Path, breakage: str) -> None:
    if breakage == "empty":
        key_file.write_text("", encoding="utf-8")
    elif breakage == "blank":
        key_file.write_text("  \n", encoding="utf-8")
    elif breakage == "empty_quotes":
        key_file.write_text('""\n', encoding="utf-8")
    elif breakage == "directory":
        key_file.mkdir()
    elif breakage == "not_utf8":
        key_file.write_bytes(b"\xff\xfe passphrase")
    elif breakage == "unreadable":
        key_file.write_text("unreadable passphrase", encoding="utf-8")
        key_file.chmod(0)
    else:
        assert breakage == "missing"


@pytest.mark.parametrize(
    "breakage",
    [
        "missing",
        "empty",
        "blank",
        "empty_quotes",
        "directory",
        "not_utf8",
        pytest.param("unreadable", marks=requires_posix_permissions),
    ],
)
def test_explicit_key_file_must_supply_the_key(monkeypatch, tmp_path, breakage):
    key_file = tmp_path / "kestrel_data_key"
    _break_key_file(key_file, breakage)
    monkeypatch.setenv(encryption.KEY_FILE_ENV_VAR_NAME, str(key_file))
    # A different valid key source must not be used in place of the explicit file.
    monkeypatch.setenv(encryption.ENV_VAR_NAME, AEADCipher.generate_key().decode("ascii"))
    monkeypatch.delenv(encryption.SALT_VALUE_ENV_VAR_NAME, raising=False)
    monkeypatch.delenv(encryption.SALT_ENV_VAR_NAME, raising=False)

    agent_did = "did:key:z6MkExplicitKeyFile"
    operations = (
        encryption.get_fernet,
        encryption.get_master_key_bytes,
        lambda: encryption.get_agent_fernet(agent_did),
        lambda: encryption.encrypt(agent_did, "conversations", b"must not be written"),
        lambda: encryption.decrypt(agent_did, "conversations", b"KSAv2:AAAA"),
    )
    try:
        for operation in operations:
            with pytest.raises(MasterKeyNotConfiguredError, match=re.escape(str(key_file))):
                operation()
    finally:
        if breakage == "unreadable":
            key_file.chmod(0o600)
    assert not _salt_path(key_file).exists()


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
