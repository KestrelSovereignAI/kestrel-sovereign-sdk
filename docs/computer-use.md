# Visual computer use and saved artifacts (contract version 1)

Parent: [Core #3703](https://github.com/KestrelSovereignAI/kestrel-sovereign/issues/3703).

`kestrel_sdk.computer_use` and `kestrel_sdk.artifacts` are contracts, not a
computer runtime. They import no browser engine, Docker client, database,
cloud SDK, web framework or inference adapter. Implementations belong in Core
and provider/product packages. These contracts neither grant access nor
enable a feature. The host derives `ResourceScope` from authenticated identity;
caller-supplied scope or provenance is never authority.

## Independent boundaries

| Boundary | Public contract | Responsibility |
| --- | --- | --- |
| Provisioning | `EnvironmentProvider` | Quote, idempotent acquire, inspect, release, authenticated host connection |
| Interaction | `ComputerDriver` | Open/status, observe, action/status, fenced pause/takeover/resume/close |
| Persistence | `ArtifactService` | Stage, commit/status, read/history/list, link/unlink/delete |
| Inference | Existing LLM contracts | Model/vision routing after host privacy checks |

Environment providers use `kestrel_sovereign.environment_providers`; drivers
use `kestrel_sovereign.computer_drivers`. Agent deployment's `cloud_providers`
and model-specific `inference_lease_providers` are different registries.
Each entry point resolves to a zero-argument factory returning its protocol
implementation; provider-specific host configuration/credentials are injected
by the host's plugin loader, never from model intent. `ComputerDriver.driver_id`
is a stable identifier. `open(request, connection)` receives the explicit
host-only `DriverConnection`; both host and driver check its driver ID, matching
lease ID, current expiry and that session expiry does not exceed connection
expiry before using any endpoint or credential. No ambient lease lookup or
secret environment-variable convention is part of this interface.
CPU environments have no model/runtime/GPU requirement. The initial contract
has a fixed authorized expiry; extending a lease is not implicitly supported
by `inspect` or reconnect. A later renewal capability needs explicit cost and
authority semantics rather than silently extending this request.

## Host admission and custody

1. Resolve tenant/principal/agent and current grants. Host file/shell consent
   does not grant visual or native-desktop control.
2. Enforce destination/privacy policy on both environment selection and all
   artifact/workspace transfers. Local-only rejects remote destinations.
3. Match actual surface/operations, a pinned image, owner, region, lifetime,
   quote expiry and price ceilings. `EnvironmentRequest.digest()` binds an
   offer to exact constraints; different decimal spellings are distinct inputs.
4. Persist request ID and accepted quote before acquiring. Reconcile an unknown
   acquire by the same ID; never launch a second resource to hide uncertainty.
5. Verify returned `quote_id` matches the accepted quote, then persist lease/resource custody before admitting an interaction session. An
   expired/failed/unknown allocation is not physical release. `released_at`
   requires provider evidence. Actual cost `None` means unknown, not zero.
6. Enforce TTL/Stop/revocation outside the driver and retain custody until
   accepted work and physical resources are reconciled.

The provider checks the same owner/request/quote at its actual boundary.
`ResourceContract` validation catches malformed records but cannot prove that
an external resource exists, an action happened, a price is correct or a grant
is current. Host services must validate returned records and their correlation.

## Session and action semantics

One active controller owns a session. `ready` permits agent control; human
takeover is `paused` with controller `human`; unknown/closing/closed fence agent
input. Every action carries the exact session, observation, control epoch and
`not_after` deadline. Host policy bounds frame age and chooses a deadline no
later than frame freshness, session and lease expiry; the driver checks that
deadline immediately before any effect. A deadline is not proof a frame is
still current: revalidate display/focus and fence changed observations too.
The driver validates current state, frame dimensions/freshness, supported action
and expiry at execution, not just at API ingress. The action ID is a durable
deduplication key; a changed payload with the same ID is a conflict. Lost
acknowledgements require `action_status`; unknown does not authorize replay.

Control requests compare the expected epoch. A handoff must drain or reconcile
accepted input, fence the previous controller, advance its epoch and then
acknowledge. Retries use the same control request ID. Reconnect never resumes
input automatically. Human takeover suspends agent observation delivery and
raw recording; handback explicitly restores them after display revalidation.

The observation contains private pixels and public frame metadata. Its digest
binds the bytes; the renderer still validates image encoding/dimensions before
serving. Generic serializers omit pixels and driver connection secrets.
Authenticated transports explicitly access them. `ActionRequest` serialization
contains private typed text/URLs and belongs only in approved encrypted stores
or transports, never a public audit log. Logs use IDs, states and reason codes.

Click intent does not prove an effect. The host/driver must enforce supported
operation scopes at application/egress boundaries and restrict ambiguous
pixel-only workflows. No implicit host access or unauthenticated public driver
endpoint is permitted by the contract.

## Saved outputs

Stage bytes under the currently authenticated `OperatorContext`. The host
maps `tenant_id`, `principal_id` and optional `acting_agent_id` exactly into
`ResourceScope`. An absent agent selects a distinct principal-owned namespace,
never a wildcard over agent artifacts. The host owns authorization, quotas, private storage
and grants. Stage returns a file digest/size/path, not public read permission.
Commit verifies every required staged blob, then commits revision metadata and
idempotency state atomically. Return a `PublicationReceipt` only after durable
acceptance. `publication_status` reconciles a lost response under the same owner
and key. `ArtifactCommit.digest()` canonically binds all commit fields, using
an order-independent manifest digest. The receipt's `request_sha256` allows
reconciliation to verify the same title, parent, provenance and files. Reuse
with another payload conflicts, even after restart.

New artifacts omit both artifact ID and expected revision. Updates require
both; the host compares the current head inside the commit transaction. A
conflict keeps recoverable staging according to bounded retention. A receipt
records historical publication, not exemption from future erasure or access
checks. Independent reads verify current policy and byte integrity.

Manifest paths are contained POSIX logical paths. Ingestion/materialization
must additionally reject symlinks, platform path aliases and archive tricks
using actual filesystem checks. Multi-file contract support does not assert
that every producer/editor accepts bundles. Derived representations identify
their exact source revision. External references remain provider-owned until
explicit export/import produces a managed snapshot.

Destroying an environment or unlinking a run cannot delete retained canonical
artifacts. Source-session/run IDs are optional attribution. Deletion compares the caller's expected head revision atomically, so a stale
view cannot erase a concurrent edit. Successful deletion retires managed derivatives, indexes and access while respecting other owners'
shared bytes and erasure policy. Uncommitted edits, credential profiles and
execution captures are not automatically published artifacts.

## Coordinated delivery

The first contract release is additive at these boundaries. Core/workflows
replace competing run-bound deliverable ownership during their coordinated
cutover; retaining operator metadata temporarily is not a second artifact
service. SDK 0.40 also contains the separately tracked native-A2A migration:
Core currently pinned below 0.40 must complete that migration before upgrading.
Do not force the upgrade into an incompatible host or add compatibility shims.
Existing Runpod/LoRA release gates stay independent of this computer-use work.
