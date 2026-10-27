"""Shared records and durable host conformance fixture; no provider/cloud IO.

The SQLite/Fernet fixture models ONE encrypted Core request row. It is not a
second production authority implementation and is never imported by SDK users.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import sqlite3
import threading
from uuid import uuid4

from cryptography.fernet import Fernet
from pydantic import SecretBytes, ValidationError
import pytest

from kestrel_sdk.computer_use import (
    CustodiedEnvironmentProvider,
    EnvironmentAcquisitionAttempt,
    EnvironmentAcquisitionCheckpoint,
    EnvironmentAcquisitionCustody,
    EnvironmentAcquisitionPreparation,
    EnvironmentAcquisitionStorage,
    EnvironmentCapabilities,
    EnvironmentLease,
    EnvironmentQuote,
    EnvironmentRequest,
    MAX_ACQUISITION_CREATE_BYTES,
    MAX_ACQUISITION_IDENTITY_BYTES,
    MAX_ACQUISITION_STORAGE_BYTES,
)
from kestrel_sdk.resources import ResourceConflict, ResourceScope
from kestrel_sdk.operator import OperatorAuthorizationError

NOW = datetime(2026, 10, 26, tzinfo=UTC)
SCOPE = ResourceScope(tenant_id="tenant", principal_id="original", agent_id="agent")


def originals():
    request = EnvironmentRequest(
        request_id="request",
        scope=SCOPE,
        provider_id="remote",
        profile_id="cpu",
        image="registry/browser@sha256:" + "a" * 64,
        surface="browser",
        location="remote",
        required_capabilities=frozenset({"screenshot", "click"}),
        requested_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        idle_ttl_seconds=60,
        ready_timeout_seconds=120,
        max_hourly_cost_usd=Decimal("1.00"),
        max_total_cost_usd=Decimal("0.10"),
    )
    quote = EnvironmentQuote(
        quote_id="quote",
        request_id=request.request_id,
        request_sha256=request.digest(),
        scope=SCOPE,
        provider_id="remote",
        location="remote",
        expires_at=NOW + timedelta(minutes=2),
        hourly_cost_usd=Decimal("0.60"),
        estimated_total_cost_usd=Decimal("0.05"),
        capabilities=EnvironmentCapabilities(
            provider_id="remote",
            location="remote",
            surfaces=frozenset({"browser"}),
            operations=frozenset({"screenshot", "click"}),
        ),
    )
    return request, quote


def preparation(payload=b'{"name":"original","cert":"private"}\n', identity=b"original-key\0\xff"):
    return EnvironmentAcquisitionPreparation(
        create_payload=SecretBytes(payload),
        worker_identity=SecretBytes(identity),
    )


def lease(**changes):
    values = dict(
        lease_id="lease",
        request_id="request",
        quote_id="quote",
        scope=SCOPE,
        provider_id="remote",
        location="remote",
        state="pending",
        expires_at=NOW + timedelta(minutes=5),
        provider_resource_id="original-native-id",
    )
    return EnvironmentLease(**(values | changes))


class EncryptedRowHost:
    """Conformance-only host: one ciphertext row, SQLite CAS, lexical check."""

    api_version = 1

    def __init__(self, path, key):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, value BLOB)")
        self.key = Fernet(key)
        self.fernet_key = key
        self.current = True
        self.allocated = 0
        self.lose_dispatch_ack = False
        self.lose_prepare_ack = False
        self.before_write = None

    def load(self):
        row = self.db.execute("SELECT value FROM requests WHERE id='request'").fetchone()
        return (
            None
            if row is None
            else EnvironmentAcquisitionStorage.deserialize_private(
                SecretBytes(self.key.decrypt(row[0]))
            )
        )

    def write(self, value, expected):
        # One atomic encrypted row CAS; sequence and exact prior ciphertext
        # prevent two provider instances from winning the same dispatch.
        prior = self.db.execute("SELECT value FROM requests WHERE id='request'").fetchone()[0]
        actual = EnvironmentAcquisitionStorage.deserialize_private(
            SecretBytes(self.key.decrypt(prior))
        )
        if actual.checkpoint != expected.checkpoint:
            raise ResourceConflict("durable checkpoint changed")
        encrypted = self.key.encrypt(value.serialize_private().get_secret_value())
        if self.before_write is not None:
            self.before_write()
        cursor = self.db.execute(
            "UPDATE requests SET value=? WHERE id='request' AND value=?", (encrypted, prior)
        )
        if cursor.rowcount != 1:
            raise ResourceConflict("durable checkpoint changed")
        self.db.commit()

    @asynccontextmanager
    async def operation(self, request, quote):
        if not self.current:
            raise OperatorAuthorizationError("original host operation revoked")
        stored = self.load()
        if stored is None:
            self.allocated += 1
            stored = EnvironmentAcquisitionStorage(
                checkpoint=EnvironmentAcquisitionCheckpoint.from_original(
                    "attempt-" + uuid4().hex,
                    request,
                    quote,
                )
            )
            self.db.execute(
                "INSERT OR IGNORE INTO requests VALUES ('request', ?)",
                (self.key.encrypt(stored.serialize_private().get_secret_value()),),
            )
            self.db.commit()
            stored = self.load()
        stored.checkpoint.require_original(request, quote)
        attempt = RetainedAttempt(self, stored)
        try:
            yield attempt
        finally:
            attempt.active = False


class RetainedAttempt:
    def __init__(self, host, stored):
        self.host, self.stored, self.active = host, stored, True

    @property
    def checkpoint(self):
        return self.stored.checkpoint

    @property
    def preparation(self):
        return self.stored.preparation

    def check_current(self):
        if not self.active or not self.host.current:
            raise OperatorAuthorizationError("original operation retired")

    async def prepare_once(self, value):
        self.check_current()
        old = self.host.load()
        candidate = EnvironmentAcquisitionStorage(
            checkpoint=old.checkpoint.prepared(value), preparation=value
        )
        self.host.write(candidate, old)
        self.stored = candidate
        if self.host.lose_prepare_ack:
            raise OSError("lost preparation acknowledgement")
        return self.preparation

    async def begin_dispatch(self):
        self.check_current()
        old = self.host.load()
        if old.checkpoint.state != "prepared":
            self.stored = old
            return False
        candidate = EnvironmentAcquisitionStorage(
            checkpoint=old.checkpoint.dispatched(),
            preparation=old.preparation,
        )
        self.host.write(candidate, old)
        self.stored = candidate
        if self.host.lose_dispatch_ack:
            raise OSError("lost checkpoint acknowledgement")
        return True

    async def record_accepted(self, value):
        return self._fact("accepted", value)

    async def record_unknown(self, value=None):
        return self._fact("unknown", value)

    def _fact(self, kind, value):
        if not self.active:
            raise OperatorAuthorizationError("original result custody retired")
        old = self.host.load()
        candidate = EnvironmentAcquisitionStorage(
            checkpoint=getattr(old.checkpoint, kind)(value),
            preparation=old.preparation,
        )
        self.host.write(candidate, old)
        self.stored = candidate
        return candidate.checkpoint


@pytest.fixture
def host(tmp_path):
    item = EncryptedRowHost(tmp_path / "canonical.sqlite", Fernet.generate_key())
    yield item
    item.db.close()


def test_explicit_private_serializer_preserves_exact_original_bytes_and_identities():
    request, quote = originals()
    original = preparation(b"\x00\xffprivate create\n", b"\xfeexact\r\nkey\x00")
    checkpoint = EnvironmentAcquisitionCheckpoint.from_original(
        "host-attempt", request, quote
    ).prepared(original)
    stored = EnvironmentAcquisitionStorage(checkpoint=checkpoint, preparation=original)
    for record in (original, checkpoint, stored):
        public = repr(record) + record.model_dump_json()
        assert "private create" not in public and "exact" not in public
        assert original.digest() not in public
    assert set(original.model_dump()) == {"schema_version"}
    assert "preparation" not in stored.model_dump()
    assert "preparation_sha256" not in checkpoint.model_dump()
    private = stored.serialize_private()
    restored = EnvironmentAcquisitionStorage.deserialize_private(private)
    assert restored == stored
    assert restored.preparation.create_payload.get_secret_value() == b"\x00\xffprivate create\n"
    assert restored.preparation.worker_identity.get_secret_value() == b"\xfeexact\r\nkey\x00"
    restored.checkpoint.require_original(request, quote)


def test_combined_maximum_preparation_round_trips_inside_storage_bound():
    request, quote = originals()
    value = preparation(b"x" * MAX_ACQUISITION_CREATE_BYTES, b"y" * MAX_ACQUISITION_IDENTITY_BYTES)
    stored = EnvironmentAcquisitionStorage(
        checkpoint=EnvironmentAcquisitionCheckpoint.from_original(
            "attempt", request, quote
        ).prepared(value),
        preparation=value,
    )
    encoded = stored.serialize_private()
    assert len(encoded.get_secret_value()) <= MAX_ACQUISITION_STORAGE_BYTES
    assert EnvironmentAcquisitionStorage.deserialize_private(encoded) == stored


def test_storage_rejects_valid_row_above_byte_limit_before_json_decode(monkeypatch):
    request, quote = originals()
    value = preparation()
    stored = EnvironmentAcquisitionStorage(
        checkpoint=EnvironmentAcquisitionCheckpoint.from_original(
            "attempt", request, quote
        ).prepared(value),
        preparation=value,
    )
    canonical = stored.serialize_private().get_secret_value()
    at_limit = canonical + b" " * (MAX_ACQUISITION_STORAGE_BYTES - len(canonical))
    assert len(at_limit) == MAX_ACQUISITION_STORAGE_BYTES
    assert EnvironmentAcquisitionStorage.deserialize_private(SecretBytes(at_limit)) == stored
    above_limit = at_limit + b" "
    assert len(above_limit) == MAX_ACQUISITION_STORAGE_BYTES + 1
    # The extra byte is valid JSON whitespace, so syntax cannot hide a lost cap.
    assert json.loads(above_limit) == json.loads(canonical)

    def decoder_must_not_run(*args, **kwargs):
        pytest.fail("oversize private row reached the JSON decoder")

    monkeypatch.setattr("kestrel_sdk.computer_use.acquisition.json.loads", decoder_must_not_run)
    with pytest.raises(ValueError, match="^private acquisition storage exceeds byte bounds$"):
        EnvironmentAcquisitionStorage.deserialize_private(SecretBytes(above_limit))


@pytest.mark.parametrize("state", ("accepted", "unknown"), ids=("accepted", "unknown"))
def test_storage_encode_cap_preserves_original_facts_and_minimal_round_trip(state):
    request, quote = originals()
    value = preparation(
        b"x" * MAX_ACQUISITION_CREATE_BYTES,
        b"y" * MAX_ACQUISITION_IDENTITY_BYTES,
    )
    checkpoint = (
        EnvironmentAcquisitionCheckpoint.from_original("attempt", request, quote)
        .prepared(value)
        .dispatched()
    )
    original_cost = Decimal("7" * 200_000)
    original_lease = lease(actual_cost_usd=original_cost)
    original_facts = original_lease.model_dump(exclude={"actual_cost_usd"})
    full = EnvironmentAcquisitionStorage(
        checkpoint=getattr(checkpoint, state)(original_lease), preparation=value
    )
    with pytest.raises(ValueError, match="^private acquisition storage exceeds byte bounds$"):
        full.serialize_private()

    minimal_lease = original_lease.model_copy(update={"actual_cost_usd": None})
    minimal = EnvironmentAcquisitionStorage(
        checkpoint=getattr(checkpoint, state)(minimal_lease), preparation=value
    )
    encoded = minimal.serialize_private()
    assert len(encoded.get_secret_value()) <= MAX_ACQUISITION_STORAGE_BYTES
    restored = EnvironmentAcquisitionStorage.deserialize_private(encoded)
    assert restored == minimal
    restored.checkpoint.require_original(request, quote)
    assert restored.checkpoint.lease.model_dump(exclude={"actual_cost_usd"}) == original_facts
    assert restored.checkpoint.lease.actual_cost_usd is None
    assert restored.preparation.create_payload.get_secret_value() == value.create_payload.get_secret_value()
    assert restored.preparation.worker_identity.get_secret_value() == value.worker_identity.get_secret_value()
    # Failing serialization or projecting unknown cost must not clamp the source.
    assert original_lease.actual_cost_usd.as_tuple() == original_cost.as_tuple()
    assert full.checkpoint.lease.actual_cost_usd.as_tuple() == original_cost.as_tuple()
    assert len(original_lease.actual_cost_usd.as_tuple().digits) == 200_000


@pytest.mark.parametrize(
    "name,limit",
    [
        ("create_payload", MAX_ACQUISITION_CREATE_BYTES),
        ("worker_identity", MAX_ACQUISITION_IDENTITY_BYTES),
    ],
)
def test_private_payload_bounds_are_inclusive_and_copied_models_revalidate(name, limit):
    original = preparation()
    values = dict(create_payload=original.create_payload, worker_identity=original.worker_identity)
    EnvironmentAcquisitionPreparation(**(values | {name: SecretBytes(b"x" * limit)}))
    for value in (b"", b"x" * (limit + 1)):
        copied = original.model_copy(update={name: SecretBytes(value)})
        with pytest.raises(ValidationError):
            copied.digest()


@pytest.mark.parametrize(
    "change",
    [
        {"max_total_cost_usd": Decimal("0.11")},
        {"max_hourly_cost_usd": Decimal("1.0")},  # Decimal spelling remains bound.
        {"scope": ResourceScope(tenant_id="tenant", principal_id="other", agent_id="agent")},
    ],
)
def test_original_request_binding_rejects_same_id_with_changed_facts(change):
    request, quote = originals()
    checkpoint = EnvironmentAcquisitionCheckpoint.from_original("attempt", request, quote)
    with pytest.raises(ResourceConflict):
        checkpoint.require_original(
            EnvironmentRequest.model_validate(request.model_copy(update=change)), quote
        )


@pytest.mark.parametrize(
    "change",
    [
        {"hourly_cost_usd": Decimal("0.61")},
        {"hourly_cost_usd": Decimal("0.6")},
        {"expires_at": NOW + timedelta(minutes=1)},
        {"quote_id": "other-quote"},
    ],
)
def test_original_quote_binding_rejects_any_changed_quote(change):
    request, quote = originals()
    checkpoint = EnvironmentAcquisitionCheckpoint.from_original("attempt", request, quote)
    with pytest.raises(ResourceConflict):
        checkpoint.require_original(request, quote.model_copy(update=change))


@pytest.mark.asyncio
async def test_restart_retains_original_attempt_payload_and_worker_identity(host):
    request, quote = originals()
    async with host.operation(request, quote) as attempt:
        assert isinstance(host, EnvironmentAcquisitionCustody)
        assert isinstance(attempt, EnvironmentAcquisitionAttempt)
        await attempt.prepare_once(preparation())
        attempt_id = attempt.checkpoint.attempt_id
        assert await attempt.begin_dispatch()
        await attempt.record_unknown()
    restarted = EncryptedRowHost(
        host.db.execute("PRAGMA database_list").fetchone()[2], host.fernet_key
    )
    try:
        async with restarted.operation(request, quote) as restored:
            assert restored.checkpoint.attempt_id == attempt_id
            assert restored.preparation == preparation()
            assert not await restored.begin_dispatch()
            await restored.record_accepted(lease())
        assert restarted.allocated == 0
        raw = host.db.execute("SELECT value FROM requests").fetchone()[0]
        assert b"original-key" not in raw and b"original-native-id" not in raw
    finally:
        restarted.db.close()


@pytest.mark.asyncio
async def test_lost_dispatch_ack_never_permits_second_native_create(host):
    request, quote = originals()
    creates = []
    async with host.operation(request, quote) as attempt:
        await attempt.prepare_once(preparation())
        host.lose_dispatch_ack = True
        with pytest.raises(OSError, match="acknowledgement"):
            if await attempt.begin_dispatch():
                creates.append("POST")
    async with host.operation(request, quote) as restored:
        assert restored.checkpoint.state == "dispatching"
        assert not await restored.begin_dispatch()
        await restored.record_unknown()
        assert restored.checkpoint.lease is None
    assert creates == [] and host.allocated == 1


@pytest.mark.asyncio
async def test_lost_preparation_ack_restores_exact_original_keys_before_dispatch(host):
    request, quote = originals()
    async with host.operation(request, quote) as attempt:
        host.lose_prepare_ack = True
        with pytest.raises(OSError, match="preparation acknowledgement"):
            await attempt.prepare_once(preparation())
    host.lose_prepare_ack = False
    async with host.operation(request, quote) as restored:
        assert restored.checkpoint.state == "prepared"
        assert restored.preparation == preparation()
        with pytest.raises(ResourceConflict):
            await restored.prepare_once(preparation(identity=b"new key"))
        assert await restored.begin_dispatch()


@pytest.mark.asyncio
async def test_concurrent_operations_share_single_atomic_dispatch(host):
    request, quote = originals()
    async with host.operation(request, quote) as original:
        await original.prepare_once(preparation())
    ready = asyncio.Event()

    async def contender():
        async with host.operation(request, quote) as attempt:
            await ready.wait()
            return attempt.checkpoint.attempt_id, await attempt.begin_dispatch()

    tasks = [asyncio.create_task(contender()) for _ in range(8)]
    await asyncio.sleep(0)
    ready.set()
    results = await asyncio.gather(*tasks)
    assert len({item[0] for item in results}) == 1
    assert sum(item[1] for item in results) == 1


@pytest.mark.asyncio
async def test_stale_row_cas_cannot_reset_first_dispatch_or_create_second_intent(host):
    request, quote = originals()
    async with host.operation(request, quote) as attempt:
        await attempt.prepare_once(preparation())
        prior = host.load()
        assert await attempt.begin_dispatch()
        with pytest.raises(ResourceConflict, match="checkpoint changed"):
            host.write(prior, prior)
        assert host.load().checkpoint.state == "dispatching"
    with pytest.raises(ResourceConflict):
        async with host.operation(
            request, quote.model_copy(update={"hourly_cost_usd": Decimal("0.61")})
        ):
            pytest.fail("changed original quote entered")
    assert host.allocated == 1


@pytest.mark.asyncio
async def test_late_native_fact_after_revocation_preserves_custody_without_authority(host):
    request, quote = originals()
    async with host.operation(request, quote) as attempt:
        await attempt.prepare_once(preparation())
        assert await attempt.begin_dispatch()
        host.current = False
        with pytest.raises(OperatorAuthorizationError):
            attempt.check_current()
        accepted = await attempt.record_accepted(lease())
        assert accepted.lease.provider_resource_id == "original-native-id"
        unknown = await attempt.record_unknown()
        assert unknown.lease == accepted.lease
        for action in (attempt.begin_dispatch(), attempt.prepare_once(preparation())):
            with pytest.raises(OperatorAuthorizationError):
                await action
    with pytest.raises(OperatorAuthorizationError):
        attempt.check_current()
    with pytest.raises(OperatorAuthorizationError):
        await attempt.record_unknown()


@pytest.mark.asyncio
async def test_changed_preparation_on_restart_refuses_and_preserves_original(host):
    request, quote = originals()
    async with host.operation(request, quote) as attempt:
        await attempt.prepare_once(preparation())
    async with host.operation(request, quote) as restored:
        for changed in (preparation(payload=b"changed"), preparation(identity=b"new-private-key")):
            with pytest.raises(ResourceConflict):
                await restored.prepare_once(changed)
        assert restored.preparation == preparation()


@pytest.mark.parametrize(
    "change",
    [
        {"lease_id": "replacement"},
        {"provider_resource_id": "replacement"},
        {"expires_at": NOW + timedelta(minutes=4)},
    ],
)
def test_unknown_and_late_fact_cannot_replace_original_native_custody(change):
    request, quote = originals()
    checkpoint = (
        EnvironmentAcquisitionCheckpoint.from_original("attempt", request, quote)
        .prepared(preparation())
        .dispatched()
        .accepted(lease())
    )
    with pytest.raises(ResourceConflict):
        checkpoint.unknown(lease(**change))
    assert checkpoint.unknown().lease == lease()


@pytest.mark.parametrize(
    "change",
    [
        {"scope": ResourceScope(tenant_id="other", principal_id="original")},
        {"quote_id": "other"},
        {"request_id": "other"},
        {"provider_id": "other"},
        {"location": "local"},
        {"expires_at": NOW + timedelta(minutes=6)},
        {"state": "released", "released_at": NOW},
    ],
)
def test_first_native_fact_cannot_change_scope_or_expiry_or_claim_release(change):
    request, quote = originals()
    checkpoint = (
        EnvironmentAcquisitionCheckpoint.from_original("attempt", request, quote)
        .prepared(preparation())
        .dispatched()
    )
    with pytest.raises(ValidationError):
        checkpoint.accepted(lease(**change))


def test_unknown_without_native_id_has_no_release_or_retry_transition():
    request, quote = originals()
    checkpoint = (
        EnvironmentAcquisitionCheckpoint.from_original("attempt", request, quote)
        .prepared(preparation())
        .dispatched()
        .unknown()
    )
    assert checkpoint.lease is None and checkpoint.state == "unknown"
    with pytest.raises(ResourceConflict):
        checkpoint.dispatched()
    with pytest.raises(ValidationError):
        checkpoint.accepted(lease(provider_resource_id=None))
    with pytest.raises(ValidationError):
        EnvironmentAcquisitionCheckpoint.model_validate(
            checkpoint.model_copy(update={"state": "released"})
        )


def test_unprepared_and_prepared_cannot_claim_dispatch_or_native_acceptance():
    request, quote = originals()
    checkpoint = EnvironmentAcquisitionCheckpoint.from_original("attempt", request, quote)
    with pytest.raises(ResourceConflict):
        checkpoint.dispatched()
    for value in (checkpoint, checkpoint.prepared(preparation())):
        with pytest.raises(ResourceConflict):
            value.accepted(lease())
        with pytest.raises(ResourceConflict):
            value.unknown()


def test_copied_invalid_originals_and_checkpoint_revalidate_at_every_helper():
    request, quote = originals()
    checkpoint = EnvironmentAcquisitionCheckpoint.from_original("attempt", request, quote)
    with pytest.raises(ValidationError):
        EnvironmentAcquisitionCheckpoint.from_original(
            "attempt", request.model_copy(update={"max_total_cost_usd": Decimal("-1")}), quote
        )
    with pytest.raises(ValidationError):
        checkpoint.require_original(
            request, quote.model_copy(update={"hourly_cost_usd": Decimal("-1")})
        )
    with pytest.raises(ValidationError):
        checkpoint.model_copy(update={"state": "accepted"}).unknown()


def test_original_unknown_lease_can_learn_native_id_without_changing_lease_identity():
    request, quote = originals()
    checkpoint = (
        EnvironmentAcquisitionCheckpoint.from_original("attempt", request, quote)
        .prepared(preparation())
        .dispatched()
        .unknown(
            lease(state="unknown", provider_resource_id=None),
        )
    )
    accepted = checkpoint.accepted(lease())
    assert accepted.lease.lease_id == checkpoint.lease.lease_id
    assert accepted.lease.provider_resource_id == "original-native-id"
    with pytest.raises(ResourceConflict):
        accepted.unknown(lease(state="unknown", provider_resource_id=None))


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b"null",
        b"{}",
        b"{",
        b"x" * (MAX_ACQUISITION_STORAGE_BYTES + 1),
        b'{"schema_version":1,"checkpoint":{},"preparation":{"create_payload":"!","worker_identity":"AA=="}}',
    ],
    # Pytest copies the node ID into PYTEST_CURRENT_TEST. Keep hostile payloads
    # out of that ID as well as diagnostics (Windows caps each env value).
    ids=(
        "json-array",
        "json-null",
        "missing-fields",
        "malformed-json",
        "over-byte-limit",
        "invalid-secret-encoding",
    ),
)
def test_storage_refuses_malformed_or_oversize_bytes_without_echoing(raw):
    with pytest.raises(ValueError, match="private acquisition storage") as error:
        EnvironmentAcquisitionStorage.deserialize_private(SecretBytes(raw))
    assert "create_payload" not in str(error.value)


def test_storage_detects_tampered_original_payload_and_strict_nested_round_trip():
    request, quote = originals()
    value = EnvironmentAcquisitionStorage(
        checkpoint=EnvironmentAcquisitionCheckpoint.from_original(
            "attempt", request, quote
        ).prepared(preparation()),
        preparation=preparation(),
    )
    raw = json.loads(value.serialize_private().get_secret_value())
    raw["preparation"]["worker_identity"] = "Y2hhbmdlZA=="
    with pytest.raises(ValueError, match="invalid private acquisition storage"):
        EnvironmentAcquisitionStorage.deserialize_private(SecretBytes(json.dumps(raw).encode()))


def test_extension_keeps_existing_protocol_and_hides_error_inputs():
    from kestrel_sdk.computer_use import EnvironmentProvider

    assert "acquire_with_custody" not in EnvironmentProvider.__dict__
    assert "acquire_with_custody" in CustodiedEnvironmentProvider.__dict__
    assert EnvironmentAcquisitionStorage.model_config["hide_input_in_errors"] is True


class BytesSubclass(bytes):
    pass


def _noncanonical_secret_values():
    import array

    return [
        bytearray(b"private"),
        memoryview(b"private"),
        array.array("B", b"private"),
        memoryview(array.array("I", range(1000))),
        memoryview(array.array("H", range(1000))),
        memoryview(bytes(600 * 1024)).cast("Q"),
        "private",
        BytesSubclass(b"private"),
    ]


@pytest.mark.parametrize("field", ["create_payload", "worker_identity"])
@pytest.mark.parametrize("raw", _noncanonical_secret_values())
def test_preparation_requires_exact_immutable_bytes_before_serialization(field, raw):
    values = dict(create_payload=SecretBytes(b"payload"), worker_identity=SecretBytes(b"identity"))
    values[field] = SecretBytes(raw)
    with pytest.raises(ValidationError, match="immutable exact bytes"):
        EnvironmentAcquisitionPreparation(**values)
    # Pydantic model_copy/construct are not authority or validation bypasses.
    forged = preparation().model_copy(update={field: SecretBytes(raw)})
    with pytest.raises(ValidationError, match="immutable exact bytes"):
        forged.digest()


@pytest.mark.parametrize("raw", _noncanonical_secret_values())
def test_private_storage_codec_requires_exact_bytes_container(raw):
    with pytest.raises(ValueError, match="private acquisition storage"):
        EnvironmentAcquisitionStorage.deserialize_private(SecretBytes(raw))


@pytest.mark.asyncio
async def test_nonbytes_preparation_cannot_poison_durable_row(host):
    import array

    request, quote = originals()
    async with host.operation(request, quote) as attempt:
        forged = preparation().model_copy(update={
            "create_payload": SecretBytes(memoryview(array.array("I", range(1000))))
        })
        with pytest.raises(ValidationError, match="immutable exact bytes"):
            await attempt.prepare_once(forged)
        assert host.load().checkpoint.state == "unprepared"
        await attempt.prepare_once(preparation())
        assert await attempt.begin_dispatch()
    async with host.operation(request, quote) as restored:
        assert restored.preparation == preparation()
        assert restored.checkpoint.state == "dispatching"
        assert not await restored.begin_dispatch()


@pytest.mark.asyncio
async def test_two_native_sqlite_connections_contend_after_read_before_dispatch_cas(host, tmp_path):
    request, quote = originals()
    async with host.operation(request, quote) as attempt:
        await attempt.prepare_once(preparation())
        original_id = attempt.checkpoint.attempt_id
    barrier = threading.Barrier(2)
    creates = []

    def contender():
        other = EncryptedRowHost(tmp_path / "canonical.sqlite", host.fernet_key)
        other.before_write = lambda: barrier.wait(timeout=5)

        async def run():
            async with other.operation(request, quote) as borrowed:
                assert borrowed.checkpoint.attempt_id == original_id
                try:
                    if await borrowed.begin_dispatch():
                        creates.append(original_id)
                        return "acknowledged"
                except ResourceConflict:
                    return "cas-conflict"
                pytest.fail("both contenders must have read the prepared row")

        try:
            return asyncio.run(run())
        finally:
            other.db.close()

    results = await asyncio.gather(asyncio.to_thread(contender), asyncio.to_thread(contender))
    assert sorted(results) == ["acknowledged", "cas-conflict"]
    assert creates == [original_id]
    assert host.load().checkpoint.state == "dispatching"
    async with host.operation(request, quote) as restored:
        assert not await restored.begin_dispatch()


@pytest.mark.parametrize("wrap", [bytearray, BytesSubclass, lambda value: value.decode()])
def test_even_valid_private_json_requires_an_exact_bytes_container(wrap):
    request, quote = originals()
    value = preparation()
    stored = EnvironmentAcquisitionStorage(
        checkpoint=EnvironmentAcquisitionCheckpoint.from_original("attempt", request, quote).prepared(value),
        preparation=value,
    )
    raw = stored.serialize_private().get_secret_value()
    assert EnvironmentAcquisitionStorage.deserialize_private(SecretBytes(raw)) == stored
    with pytest.raises(ValueError, match="private acquisition storage"):
        EnvironmentAcquisitionStorage.deserialize_private(SecretBytes(wrap(raw)))
