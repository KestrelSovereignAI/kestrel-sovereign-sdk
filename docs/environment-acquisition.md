# Optional environment acquisition custody, API version 1

Version 0.45.0 combines acquisition custody with the human-control contracts
introduced in 0.44.0. Concrete host/provider integration requires separate
implementation and verification; these contracts alone do not enable it.
It does not enable a provider, implement a Core store or authorize cloud use.
`EnvironmentProvider` and its entry-point group remain unchanged. An optional
`CustodiedEnvironmentProvider.acquire_with_custody(request, quote, custody)`
uses a trusted host `EnvironmentAcquisitionCustody` with `api_version == 1`.
A provider requiring this extension refuses legacy `acquire` explicitly. A
provider must not advertise usable acquisition until the compatible host,
transport, image and lifecycle boundaries compose and are tested.

## Original operation and durable preparation

`custody.operation(original_request, original_quote)` is an async context manager
yielding an `EnvironmentAcquisitionAttempt`. Core allocates and persists its
`attempt_id` once on the SAME canonical encrypted request row before yielding.
The provider cannot supply a new attempt ID on restart. The checkpoint binds
the exact request and quote, including Decimal spelling, scope, provider,
location and request expiry. It is a factual record, not a bearer token.

The attempt exposes read-only `checkpoint` and `preparation` views. When no
preparation exists, the provider constructs one exact `create_payload` and
`worker_identity` and calls `await attempt.prepare_once(preparation)`. Both
fields are nonempty `SecretBytes` wrapping exact immutable `bytes` (including
no bytes subclasses): at most 512 KiB and 128 KiB respectively. Mutable buffers,
memoryviews, arrays and strings are refused before any persistence or digest.
The identity contains all original per-request TLS keys/certificates and other
worker credentials needed after a restart. The payload contains the EXACT
native create bytes referencing those identities. A retry uses the original
view; it never regenerates keys, re-encodes the create, or changes its digest.
Changed preparation conflicts. No native create precedes durable preparation.

`attempt.check_current()` is the original synchronous host checker, valid only
within the lexical operation. The host retains its authenticated owner,
current grants, privacy, lifecycle and original request/quote policy. IDs,
attribution and checkpoint facts cannot construct another checker. The actual
native HTTP transport must call this original checker immediately before its
final write, after awaited DNS/connect/TLS/process work, and retain it through
private response delivery. Sampling it before an await is insufficient.

## At-most-once dispatch and reconciliation

The pure checkpoint helpers produce CAS candidates; they are not transactions.
The host serializes operation entries or atomically compares the same row and
sequence. The durable states are:

| Original state | Allowed candidate | Meaning |
| --- | --- | --- |
| unprepared | prepared | Original payload and identity persisted together |
| prepared | dispatching | One atomic first-dispatch CAS, ever |
| dispatching | accepted or unknown | Original native result retained |
| unknown | accepted or unknown | Reconcile the SAME attempt, never replay |
| accepted | accepted or unknown | Preserve the original native resource |

`await attempt.begin_dispatch()` returns `True` only after that first durable
CAS. `False` means reconciliation only. Any exception or lost acknowledgement
forbids native create, even if no POST was actually sent. Restart reads
`dispatching` and reconciles; it cannot reset the state, prepare another intent
or invent an idempotency key. Version 1 has no terminal rejection or reset after
the dispatch CAS, even for a definitive native rejection or an abort before
sending any bytes. Such an attempt remains dispatching/unknown; without an
original accepted lease, the provider raises `ResourceOutcomeUnknown`, never
a fabricated lease. It cannot create again even if native absence is known.
This conservative availability limit prevents duplicate paid capacity.

`record_accepted(lease)` requires a known original native resource.
`record_unknown(lease=None)` preserves any existing lease/native ID. An unknown
placeholder lease may learn its native ID while keeping its lease identity and
expiry. Known IDs cannot be replaced or cleared; expiry cannot be extended and
scope/request/quote/provider/location cannot change. Acquisition has no released
state: the existing canonical lease lifecycle owns verified physical release.
An unknown create with no native ID remains unresolved after an empty inventory.
Neither deterministic names nor eventual list absence prove create failure.

Late `record_accepted`/`record_unknown` may persist the EXACT original native
result after authority loss while the lexical operation still owns that result.
They authorize no I/O, retry, observation, credential delivery or fresh grant.
The original checker continues to deny. After operation exit, even factual
recording through the borrowed attempt is refused. Hosts join/custody native
effects before exiting; they must not detach a provider task and discard its
possible result. Physical cleanup uses the existing original custody path.

## Private encrypted-row serialization

`EnvironmentAcquisitionStorage(checkpoint=..., preparation=...)` verifies that
the private exact-byte preparation digest matches the checkpoint. Generic
`model_dump`, JSON and repr omit both preparation fields and that digest.
Those dumps are deliberately NOT restart persistence formats.

`storage.serialize_private() -> SecretBytes` explicitly encodes all original
facts and bounded byte strings, using base64 without interpreting/re-encoding
the provider payload. `EnvironmentAcquisitionStorage.deserialize_private(...)`
validates the strict nested records, original digest and byte bounds. The
entire storage encoding is at most 1 MiB, checked before decoding. The SDK does
not encrypt it: Core must encrypt these bytes under the existing request key,
in the SAME atomic row transaction as request/lease custody. The private codec
must never be sent as tool output, logged or stored in a second provider store.

The codec limit is not a promise that every valid `EnvironmentLease` fits:
its inherited optional `actual_cost_usd` Decimal has no digit limit. Hosts must
persist bounded minimal native custody on the SAME encrypted request row before
optional billing enrichment. If that cost cannot fit, explicitly project only
`actual_cost_usd=None` while retaining every other original lease fact, especially
the native resource ID and expiry. `None` means unknown cost, never zero or a
budget approval. Keep the unmodified provider observation private in its original
operation; the codec does not silently strip, normalize, round or clamp it.
Optional billing validation or a serialization failure must not discard known
native custody, reset dispatch or permit another create. The concrete host
adapter must verify this ordering; the SDK does not implement that adapter.

Preparation/content digests and structured validation details are private.
SDK error strings suppress input values, but unknown field names can remain;
non-SDK enclosing models and TypeAdapters need their own hiding configuration.
Log exception classes only. Pure validation and a successful TLS exchange are
neither caller authentication nor proof of remote sandbox/readiness.

The SDK tests include a conformance-only encrypted SQLite row fixture proving
restart identity, lost acknowledgements, original payload conflict, unknown
custody and lexical revocation. A two-thread/two-connection SQLite test pauses
both contenders after reading the same ciphertext and before its UPDATE CAS,
then proves only one acknowledged dispatch. The simpler event-loop contenders
also share the original attempt, but alone do not prove a CAS race. That fixture is not
Core integration, production HTTP final-write validation, remote worker
conformance, AMD64 image evidence or a paid Runpod trial.
