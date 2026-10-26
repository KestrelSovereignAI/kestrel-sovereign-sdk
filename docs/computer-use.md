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
| Persistence | `ArtifactService` | Stage, commit/status, head/read/history/list, link/unlink/delete |
| Inference | Existing LLM contracts | Model/vision routing after host privacy checks |

Environment providers use `kestrel_sovereign.environment_providers`; drivers
use `kestrel_sovereign.computer_drivers`. Agent deployment's `cloud_providers`
and model-specific `inference_lease_providers` are different registries.
Each entry point resolves to `EnvironmentProviderFactory` or
`ComputerDriverFactory`, invoked with keyword-only `settings: Mapping[str,
object]` from trusted host configuration. Each package validates its private
settings; neither configuration nor credentials come from model intent or
enter generic serialization/logs. `ComputerDriver.driver_id`
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

`ResourceScope.agent_id` identifies an optional owning agent, not the active
controller identity. Principal-owned automation may have no owning agent;
`controller="agent"` names the automation role. The host still authenticates
the actual invoking principal/actor and grants on every operation. An absent
owning agent never widens that authority.

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

## Optional human control (SDK 0.44.0)

`HumanControlProvider.human_control()` negotiates the optional `HumanControl`
transport alongside the session's `takeover` capability. Missing either refuses
manual pixels/input. This additive source is not part of published SDK 0.43.0.
No provider advertises support until it implements this contract and a reviewed
SDK release containing it is actually published and locked by the consumer.
The source candidate is version 0.44.0; building it does not publish a release.
There is no new entry point, browser/CDP endpoint or agent-act compatibility shim.

The host first authenticates an operator, acknowledges `ComputerDriver.control`
takeover and persists the paused/human session at its new epoch. It resolves a
`consumer_id` from that exact authenticated operator and creates a
`HumanControlRequest` with the original owner, session/resource lease/driver,
control ID/epoch, fixed issue/expiry, capability subset and maximum frame age.
These records grant no permission. An attribution frame, known consumer ID,
old lease or previous receipt cannot authenticate an operator or grant access.
The host holds original current grants/privacy and exact consumer binding
through delivery and effects. Driver connection authentication alone does not
establish the human consumer: the authenticated host transport must bind each
call to the admitted consumer, and the worker verifies its installed lease.

`open(session, request)` durably installs the canonical request digest. One
consumer lease is allowed per epoch; no replacement, renewal or reconnect may
restart input. Identical retries reconcile the original binding. Changed
retries conflict, while expired/revoked IDs never reopen. A new consumer needs
a fresh explicit takeover epoch. Lease expiry must not exceed the admitted
session or original driver connection expiry. A lease state of `active` in a
stored snapshot is not proof that the worker's installed lease remains active.

`observe(session, lease)` returns `HumanScreenObservation`: exact consumer and
control IDs around the canonical private `ScreenObservation`. Its observation
namespace is distinct from agent frames and other consumers. `act` accepts
`HumanActionRequest`, which wraps the same canonical action payload with exact
consumer/control IDs; this uses a dedicated authenticated human dispatch, never
the agent `act` method. The driver rechecks current installed lease, epoch,
expiry, current frame/display/focus, capability, coordinates and deadline just
before effect. Maximum frame age and fixed expiry also bound `not_after`.
Canonical navigation syntax never grants destination/egress access.

`observation.frame_record` yields a pixel-free `HumanScreenFrame`, which contains
only consumer/control IDs and canonical `ScreenFrame` metadata. Persist or pass
that record to `HumanActionRequest.require_lease` rather than retaining private
pixels. It roundtrips through JSON without image bytes. The driver still compares
these supplied facts to its installed lease and current display; frame metadata
alone cannot prove currency. Host policy must choose a suitable maximum frame
age and lifetime: the SDK checks internal consistency, not a universal duration.

The driver persists human input intent and its private digest before effect and
retains its original action ID and `HumanActionReceipt`. Action IDs cannot alias an agent
request or another consumer/lease with different content. Unknown input is
reconciled with `action_status(session, lease, original_action_id)` and never
automatically replayed. `status(session, original_request)` returns the original
lease binding; the host checks its digest with `require_request`. Status calls
require current metadata-read policy, including after expiry/revocation. A
missing record raises `ResourceNotFound` and does not prove an in-flight call
cannot finish. No reconciliation operation exposes private pixels or adds input.

`revoke(session, HumanControlRevocation)` targets the original consumer/control
ID and original `control_epoch` even after expiry or a later session epoch. Unlike
`ControlRequest.expected_control_epoch`, this names the original lease epoch,
not the current session epoch. The revocation digest binds
the retirement request ID and complete target for idempotent retries. Authenticated
original custody permits retirement, not new data/effects. The priority control
path fences and drains/reconciles accepted input before `revoked` is confirmed;
timeouts remain `unknown`/`revoking`, retaining original lease and receipt custody.
Stop, takeover, close and release must not queue behind ordinary input/privacy
admission. Handback revokes human leases, drains/reconciles input, advances the
epoch and revalidates display before agent observation/input resumes. Uncertain
input prevents handback until reconciled or its original process is retired.

Revoking an unknown control ID must atomically record a durable pre-open fence
before returning `HumanControlRevocationTombstone`. The record correlates the
original revocation and exact scope/resource lease/driver; it does not claim a
lease was accepted. It may be returned only when no open was accepted. Every
in-flight or late `open` compares that fence in its acceptance transaction.
The fenced ID can never install a lease, including
changed consumer/payload retries. Repeated revoke reconciles the original request
ID and target. Absence of an open record alone is insufficient; possibly accepted
work retains the original lease as unknown/revoking until drained/reconciled.
The tombstone's `require_revocation` helper correlates supplied records; only
the worker's actual durable transaction can establish that the fence exists.
`status` for a pre-open tombstoned control ID raises `ResourceConflict` rather
than claiming no record exists. Retry the original revoke to reconcile the
fence; status of an accepted open still returns its exact original lease.

The SDK's `require_session`, `require_active` and `require_lease` helpers validate
live snapshot facts using a trusted aware clock. `require_request`,
`require_target` and `require_revocation` correlate original records without
granting authority or asserting freshness. None proves authentication, installed
worker state, frame currency or physical effects. A consumer must still validate returned records and invoke
these checks at the real boundary. Generic observation serialization omits
pixels. Human action serialization contains private text/URLs and belongs only
in approved encrypted stores/transports, never logs or model context during
takeover. The action content digest is private too: an unsalted hash can confirm
guesses of short secrets. Do not expose/log it; durable receipt authentication
needs a host-keyed digest inside encrypted custody. Direct validation of SDK
resource records hides inputs in error text. Any non-SDK enclosing model or
`TypeAdapter` must also set
`hide_input_in_errors=True` (or validate the private record separately first);
the outer validator controls error formatting. SDK `ResourceContract` wrappers
inherit this setting. Pydantic `errors()`/`json()` details may still retain inputs
and must not be logged. Shared digest date normalization rejects instants whose
conversion to UTC falls outside years 1–9999 with `ValueError`, allowing enclosing
validators to report a normal validation failure rather than raw overflow.
Computer, human-control and workspace datetime fields normalize seconds-level
or fractional UTC offsets to UTC before serialization, because JSON serializers
cannot retain those offsets exactly. Whole-minute offset representations and
existing valid digests remain unchanged. Concrete host/worker/UI integration
and adversarial transport tests remain separate from these pure SDK source contracts.

## Saved outputs

Stage bytes under the currently authenticated `OperatorContext`. The host
maps `tenant_id`, `principal_id` and optional `acting_agent_id` exactly into
`ResourceScope`. An absent agent selects a distinct principal-owned namespace,
never a wildcard over agent artifacts. The host owns authorization, quotas, private storage
and grants. Stage returns a file digest/size/path, not public read permission.
Stage validates `publication_key` as an `Identifier`, identical to the future
commit's idempotency key. It binds the exact path, digest, size and media type to that save attempt;
identical retries reuse staging and a changed file at the same key/path
conflicts while the staging binding is live. Hosts may expire uncommitted
staging; callers retain original bytes and restage identical files under the
original key before retrying. A completed or erased publication key rejects
staging with `ResourceConflict`, including an identical restage. Reconcile with
`publication_status` or retry the identical commit; this staging conflict alone
does not mean publication failed. An erased key is terminal: both status and
commit raise `ResourceNotFound`, without resurrecting a receipt or its bytes.
Commit verifies every required file was staged for that exact publication,
then commits revision metadata and idempotency state atomically. Return a `PublicationReceipt` only after durable
acceptance. `publication_status` reconciles a lost response under the same owner
and key. `ArtifactCommit.digest()` canonically binds all commit fields, using
an order-independent manifest digest. The receipt's `request_sha256` allows
reconciliation to verify the same title, parent, provenance and files. Reuse
of a durably accepted key with another payload conflicts, even after restart.

A status of `None` means no receipt is currently visible; an in-flight commit
may still complete. Retain staging and reconcile/retry the identical commit
with its original key. Never mint a new key or discard unresolved staging on
that observation alone.

New artifacts omit both artifact ID and expected revision. Updates require
both; the host compares the current head inside the commit transaction. A
confirmed rolled-back compare-and-swap conflict does not bind the commit key
and keeps staging only for bounded recovery. Once all attempts with that key
are resolved, the caller may rebase `expected_revision_id` with the same key
and staged files. Choosing a new publication key requires restaging every file
under that key. Retain source bytes until saved or explicitly discarded. A
frozen export counts only while its session and lease are live; otherwise the
broker retains an authorized bounded copy. Host staging may expire.
Unknown outcomes never authorize changing the request. A receipt
records historical publication, not exemption from future erasure or access
checks. Independent reads verify current policy and byte integrity.

Editors resolve a conflict against `ArtifactService.head(context, artifact_id)`.
It returns the committed current revision in the caller's exact owner namespace;
missing or deleted artifacts raise `ResourceNotFound`. History ordering is for
browsing and cannot establish the head: clocks can move backward or tie. Head
reads remain subject to current access/privacy policy and grant no write lock.
The next publication must still compare that revision using
`expected_revision_id`, and a concurrent writer can cause another conflict.

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

## Workspace transfer

`kestrel_sdk.workspaces.WorkspaceTransfer` is an optional driver extension,
negotiated through `WorkspaceTransferProvider.workspace_transfer()` alongside
its advertised `files` capability. Missing either refuses transfer. There is
no unrestricted shell fallback and no new public transport implied.

Core's artifact service owns the broker: it authorizes the source session or
pinned artifact, validates destination policy, and uses the same authenticated
`OperatorContext` for staging/commit/read. Environment providers only provision.
The driver owns actual regular-file containment, bounded reads/writes, frozen
export snapshots, staging cleanup and atomic installation into a fresh target.
Export selection is explicit files, never glob/archive expansion. Symlinks,
hard-link escapes, devices and aliases are rejected at the filesystem boundary.

Data operations require a paused session with controller `none`, the exact
lease/control epoch and a live deadline bounded by session/lease expiry. Human
takeover or revocation fences transfer data immediately. Authenticated cleanup
retains authority to retire admitted staging after expiry; it grants no reads.

Prepare binds the transfer ID to `request.digest()`. Retries with changed
content conflict; unknown outcomes reconcile the same ID. Export status carries
the original snapshot in ready/transferring state. Identical `prepare_export`
retries return that same snapshot and never freeze again, even if processes in
the workspace have since changed files. Once released, the same prepare ID
raises `ResourceNotFound`; while releasing it raises `ResourceConflict`.
An identical `prepare_import` retry after complete returns the original status
and receipt without reinstalling; while aborting it raises `ResourceConflict`,
and after aborted it raises `ResourceNotFound` without creating staging. Current
metadata-read authority is still required for reconciliation.
Export retirement uses releasing/released; import
retirement uses aborting/aborted; only imports have a complete state.
Exports freeze a manifest with verified bytes; bounded `SecretBytes` reads do not expose content
through generic representations. Materialization verifies every manifest file
and atomically installs the complete source revision in a new target. An
existing target conflicts. A complete import requires a
`WorkspaceMaterialization` receipt; partial staging is never a live workspace.

A snapshot/materialization receipt does not prove a saved artifact. Only
`ArtifactService.commit` returns durable publication evidence. Releasing an
export, aborting import staging, destroying a machine and deleting a canonical
artifact are separate lifecycle operations.

## Coordinated delivery

The first contract release is additive at these boundaries. Core/workflows
replace competing run-bound deliverable ownership during their coordinated
cutover; retaining operator metadata temporarily is not a second artifact
service. SDK 0.40 also contains the separately tracked native-A2A migration:
Core currently pinned below 0.40 must complete that migration before upgrading.
Do not force the upgrade into an incompatible host or add compatibility shims.
Existing Runpod/LoRA release gates stay independent of this computer-use work.
