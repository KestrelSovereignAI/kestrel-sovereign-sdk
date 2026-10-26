"""Optional host-owned acquisition custody for non-idempotent native creates.

Records are facts, never authority. The host retains its original authenticated
operation and stores this preparation on its existing encrypted request row.
No provider store, network client, encryption key or retry worker lives here.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import AbstractAsyncContextManager
import hashlib
import json
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import Field, SecretBytes, model_validator

from kestrel_sdk._validation import canonical_utc_datetime
from kestrel_sdk.resources import (
    Identifier,
    NonNegativeInt,
    ResourceConflict,
    ResourceContract,
    ResourceScope,
)
from .models import (
    EnvironmentLease,
    EnvironmentQuote,
    EnvironmentRequest,
    ImageDigest,
    Location,
    _WireAwareDatetime,
)
from .protocols import EnvironmentProvider

MAX_ACQUISITION_CREATE_BYTES = 512 * 1024
MAX_ACQUISITION_IDENTITY_BYTES = 128 * 1024
# Base64 expansion plus bounded records/escaping. Decode checks before allocation.
MAX_ACQUISITION_STORAGE_BYTES = 1024 * 1024


class EnvironmentAcquisitionPreparation(ResourceContract):
    """Exact original native payload and worker identity; both PRIVATE bytes.

    The identity includes every original private key/certificate/credential
    needed by this attempt. Providers must never regenerate either byte string
    on restart. Payload encoding is provider-owned; these are not bearer grants.
    Generic dumps/repr omit both fields. Detailed validation errors are private.
    """

    create_payload: SecretBytes = Field(exclude=True, repr=False)
    worker_identity: SecretBytes = Field(exclude=True, repr=False)

    @model_validator(mode="after")
    def bounded_bytes(self) -> Self:
        for value, limit in (
            (self.create_payload, MAX_ACQUISITION_CREATE_BYTES),
            (self.worker_identity, MAX_ACQUISITION_IDENTITY_BYTES),
        ):
            if not 0 < len(value.get_secret_value()) <= limit:
                raise ValueError("acquisition preparation exceeds private byte bounds")
        return self

    def digest(self) -> str:
        """PRIVATE exact-byte correlation only; never log or authorize by it."""
        original = type(self).model_validate(self)
        digest = hashlib.sha256(b"kestrel-environment-preparation-v1\0")
        for value in (original.create_payload, original.worker_identity):
            raw = value.get_secret_value()
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
        return digest.hexdigest()


def _quote_digest(quote: EnvironmentQuote) -> str:
    payload = quote.model_dump(mode="json")
    payload["expires_at"] = canonical_utc_datetime(quote.expires_at)
    for name in ("surfaces", "operations"):
        payload["capabilities"][name] = sorted(getattr(quote.capabilities, name))
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class EnvironmentAcquisitionCheckpoint(ResourceContract):
    """Host allocated attempt facts; CAS this on the SAME durable request row.

    A known native resource never disappears from this checkpoint. Physical
    release remains the canonical lease lifecycle, outside acquisition. Unknown
    without a native ID stays unresolved, including after an empty inventory.
    """

    attempt_id: Identifier
    request_id: Identifier
    request_sha256: ImageDigest
    quote_id: Identifier
    quote_sha256: ImageDigest
    scope: ResourceScope
    provider_id: Identifier
    location: Location
    expires_at: _WireAwareDatetime
    sequence: NonNegativeInt = 0
    state: Literal["unprepared", "prepared", "dispatching", "accepted", "unknown"] = "unprepared"
    preparation_sha256: ImageDigest | None = Field(default=None, exclude=True, repr=False)
    lease: EnvironmentLease | None = None

    @model_validator(mode="after")
    def correlated_facts(self) -> Self:
        if (self.state == "unprepared") != (self.preparation_sha256 is None):
            raise ValueError("checkpoint preparation state does not match original digest")
        if self.state in {"unprepared", "prepared", "dispatching"} and self.lease is not None:
            raise ValueError("undispatched checkpoint cannot claim native acceptance")
        if self.state == "accepted" and (
            self.lease is None or self.lease.provider_resource_id is None
        ):
            raise ValueError("accepted checkpoint requires original native resource custody")
        if self.lease is not None:
            lease = self.lease
            if (
                lease.request_id,
                lease.quote_id,
                lease.scope,
                lease.provider_id,
                lease.location,
            ) != (self.request_id, self.quote_id, self.scope, self.provider_id, self.location):
                raise ValueError("checkpoint lease differs from original acquisition")
            if lease.state == "released":
                raise ValueError("physical release is recorded by the canonical lease lifecycle")
            if lease.expires_at > self.expires_at:
                raise ValueError("native fact extends the original acquisition expiry")
        return self

    @classmethod
    def from_original(
        cls,
        attempt_id: str,
        request: EnvironmentRequest,
        quote: EnvironmentQuote,
    ) -> Self:
        """Core allocates attempt_id once BEFORE yielding a provider operation."""
        request = EnvironmentRequest.model_validate(request)
        quote = EnvironmentQuote.model_validate(quote)
        if (
            quote.request_id,
            quote.request_sha256,
            quote.scope,
            quote.provider_id,
            quote.location,
        ) != (
            request.request_id,
            request.digest(),
            request.scope,
            request.provider_id,
            request.location,
        ):
            raise ResourceConflict("acquisition quote differs from original request")
        return cls(
            attempt_id=attempt_id,
            request_id=request.request_id,
            request_sha256=request.digest(),
            quote_id=quote.quote_id,
            quote_sha256=_quote_digest(quote),
            scope=request.scope,
            provider_id=request.provider_id,
            location=request.location,
            expires_at=request.expires_at,
        )

    def require_original(self, request: EnvironmentRequest, quote: EnvironmentQuote) -> None:
        original = type(self).model_validate(self)
        expected = type(self).from_original(original.attempt_id, request, quote)
        for name in (
            "attempt_id",
            "request_id",
            "request_sha256",
            "quote_id",
            "quote_sha256",
            "scope",
            "provider_id",
            "location",
            "expires_at",
        ):
            if getattr(original, name) != getattr(expected, name):
                raise ResourceConflict("checkpoint differs from original acquisition")

    def prepared(self, preparation: EnvironmentAcquisitionPreparation) -> Self:
        """Pure candidate transition; host persists atomically before any IO."""
        original = type(self).model_validate(self)
        digest = EnvironmentAcquisitionPreparation.model_validate(preparation).digest()
        if original.preparation_sha256 is not None:
            if original.preparation_sha256 != digest:
                raise ResourceConflict("acquisition preparation differs from original bytes")
            return original
        return original.model_copy(
            update={
                "state": "prepared",
                "preparation_sha256": digest,
                "sequence": original.sequence + 1,
            }
        )

    def dispatched(self) -> Self:
        """Pure CAS candidate. Only one durable prepared->dispatching CAS wins.

        A failed/lost CAS acknowledgement forbids POST. A later operation sees
        dispatching and may reconcile facts, never dispatch a replacement.
        """
        original = type(self).model_validate(self)
        if original.state != "prepared":
            raise ResourceConflict("original acquisition has already begun or is unprepared")
        return original.model_copy(
            update={"state": "dispatching", "sequence": original.sequence + 1}
        )

    def accepted(self, lease: EnvironmentLease) -> Self:
        return self._native_fact("accepted", lease)

    def unknown(self, lease: EnvironmentLease | None = None) -> Self:
        return self._native_fact("unknown", lease)

    def _native_fact(self, state: str, lease: EnvironmentLease | None) -> Self:
        original = type(self).model_validate(self)
        if original.state not in {"dispatching", "accepted", "unknown"}:
            raise ResourceConflict("native fact has no original dispatch custody")
        if lease is None:
            lease = original.lease
        else:
            lease = EnvironmentLease.model_validate(lease)
        if original.lease is not None and lease is not None:
            if (lease.lease_id, lease.expires_at) != (
                original.lease.lease_id,
                original.lease.expires_at,
            ) or (
                original.lease.provider_resource_id is not None
                and lease.provider_resource_id != original.lease.provider_resource_id
            ):
                raise ResourceConflict("native fact replaces original resource custody")
        values = original.model_dump()
        values.update(
            state=state,
            lease=lease,
            preparation_sha256=original.preparation_sha256,
            sequence=original.sequence + 1,
        )
        return type(self).model_validate(values)


class EnvironmentAcquisitionStorage(ResourceContract):
    """Encrypted-row value; public dumps intentionally cannot reconstruct it.

    The host stores the explicit serializer result under its existing request
    encryption key/transaction. The SDK serializer does not itself encrypt.
    """

    checkpoint: EnvironmentAcquisitionCheckpoint
    preparation: EnvironmentAcquisitionPreparation | None = Field(
        default=None, exclude=True, repr=False
    )

    @model_validator(mode="after")
    def exact_preparation(self) -> Self:
        expected = self.checkpoint.preparation_sha256
        if (self.preparation is None) != (expected is None):
            raise ValueError("storage is missing original private preparation")
        if self.preparation is not None and self.preparation.digest() != expected:
            raise ValueError("storage preparation differs from original digest")
        return self

    def serialize_private(self) -> SecretBytes:
        """Exact bytes for ENCRYPTED persistence only; never log/send publicly."""
        original = type(self).model_validate(self)
        checkpoint = original.checkpoint.model_dump(mode="json")
        checkpoint["preparation_sha256"] = original.checkpoint.preparation_sha256
        preparation = None
        if original.preparation is not None:
            preparation = {
                name: base64.b64encode(
                    getattr(original.preparation, name).get_secret_value()
                ).decode("ascii")
                for name in ("create_payload", "worker_identity")
            }
        encoded = json.dumps(
            {"schema_version": 1, "checkpoint": checkpoint, "preparation": preparation},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > MAX_ACQUISITION_STORAGE_BYTES:
            raise ValueError("private acquisition storage exceeds byte bounds")
        return SecretBytes(encoded)

    @classmethod
    def deserialize_private(cls, value: SecretBytes) -> Self:
        """Load DECRYPTED row bytes; revalidate exact original identities/bytes."""
        if not isinstance(value, SecretBytes):
            raise ValueError("private acquisition storage requires secret bytes")
        raw = value.get_secret_value()
        if not 0 < len(raw) <= MAX_ACQUISITION_STORAGE_BYTES:
            raise ValueError("private acquisition storage exceeds byte bounds")
        try:
            values = json.loads(raw)
            if not isinstance(values, dict):
                raise ValueError
            preparation = values.get("preparation")
            if preparation is not None:
                if not isinstance(preparation, dict) or set(preparation) != {
                    "create_payload",
                    "worker_identity",
                }:
                    raise ValueError
                values["preparation"] = {
                    name: SecretBytes(base64.b64decode(payload, validate=True))
                    for name, payload in preparation.items()
                }
            # JSON mode preserves strict nested Decimal/datetime/set contracts.
            checkpoint = values.get("checkpoint")
            values["checkpoint"] = EnvironmentAcquisitionCheckpoint.model_validate_json(
                json.dumps(checkpoint)
            )
            return cls.model_validate(values)
        except (ValueError, TypeError, binascii.Error, RecursionError):
            raise ValueError("invalid private acquisition storage") from None


@runtime_checkable
class EnvironmentAcquisitionAttempt(Protocol):
    """One lexical borrowed operation; the original checker expires on exit.

    prepare_once/begin_dispatch require original authority, policy and unexpired
    original request/quote through their atomic commit. begin_dispatch returns
    True once EVER, only after durable CAS; False
    means reconciliation only. Exceptions, including lost commit acknowledgments,
    NEVER permit native dispatch. Concurrent entries serialize or use that CAS.

    Late record_accepted/record_unknown may retain exact original native facts
    after authority loss while this lexical operation still owns the result.
    They do not call check_current or permit new IO. They cannot replace known
    identities, clear custody, extend expiry, or create a fresh attempt.
    """

    @property
    def checkpoint(self) -> EnvironmentAcquisitionCheckpoint: ...
    @property
    def preparation(self) -> EnvironmentAcquisitionPreparation | None: ...
    def check_current(self) -> None: ...
    async def prepare_once(
        self, value: EnvironmentAcquisitionPreparation
    ) -> EnvironmentAcquisitionPreparation: ...
    async def begin_dispatch(self) -> bool: ...
    async def record_accepted(
        self, lease: EnvironmentLease
    ) -> EnvironmentAcquisitionCheckpoint: ...
    async def record_unknown(
        self, lease: EnvironmentLease | None = None
    ) -> EnvironmentAcquisitionCheckpoint: ...


@runtime_checkable
class EnvironmentAcquisitionCustody(Protocol):
    """Versioned optional trusted host capability; never derive it from IDs.

    Core allocates/persists attempt_id ONCE on the original encrypted request
    row before yielding, validates exact original request/quote, and retains
    original current authority/privacy/lifecycle through provider IO and final
    delivery. The provider checks attempt.check_current at its actual final
    native write, after every awaited connect/launch. It may not invent a
    checker from a frozen context or use checkpoint facts as authority.
    """

    @property
    def api_version(self) -> Literal[1]: ...
    def operation(
        self,
        request: EnvironmentRequest,
        quote: EnvironmentQuote,
    ) -> AbstractAsyncContextManager[EnvironmentAcquisitionAttempt]: ...


@runtime_checkable
class CustodiedEnvironmentProvider(EnvironmentProvider, Protocol):
    """Optional extension for APIs lacking a native idempotent create contract.

    Hosts use this method instead of acquire when negotiated. Providers needing
    this contract must explicitly refuse legacy acquire, never create a private
    authority store or silently retry. Ordinary provider/driver contracts and
    plugin groups remain unchanged; implementing this is not registration.
    """

    async def acquire_with_custody(
        self,
        request: EnvironmentRequest,
        quote: EnvironmentQuote,
        custody: EnvironmentAcquisitionCustody,
    ) -> EnvironmentLease: ...
