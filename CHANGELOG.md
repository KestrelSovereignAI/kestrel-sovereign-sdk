# Changelog

All notable changes to this project are documented in this file.

## 0.37.0 — 2026-08-25

### Added

- Added the public `INBOUND_PRODUCER_CAPABILITY` contract and
  `IsolatedFeatureService.advertise_inbound_producer()` so services can declare
  explicitly whether host idle retirement would remove an unmanaged poller or
  listener. Omitted declarations remain ambiguous for backward-compatible,
  fail-resident host policy.
- Added typed client accessors that normalize absent or malformed declarations
  to the fail-resident state and authorize idle retirement only for exact
  `False`.
- Froze producer ownership after successful initialize negotiation so a live
  config change cannot leave a stale retirement-safe declaration on the host;
  producer-state changes require restart and re-negotiation.

## 0.36.0 — 2026-08-10

### Added

- Added exact, versioned service discovery contracts and trusted operator
  context, execution-target discovery, and server-side target authorization.
- Added durable run, control, stage, attempt, external-job, and authorized
  artifact protocols for feature-owned operator execution.
- Added `Feature` and `HostFeature` contribution methods
  (`get_service_registrations()`, `get_wait_provider_registrations()`,
  `get_workflow_registrations()`, `get_feature_permission_defaults()`, and
  `get_setup_step_registrations()`) with canonical validated
  `contribution_owner` identities. The module-qualified implementation default
  is deterministic, independent of mutable feature names, distinguishes equal
  class names in separate packages and `_Foo` from `Foo`, and uses a bounded
  hash fallback for unusual names. The public active-set validator requires
  runtimes to reject duplicate owners before registration. The base classes do
  not claim or intercept the historic `owner` attribute, so existing
  subclasses may continue assigning it any value. Runtimes validate
  registration owner fields against the exact canonical identity and retain
  that identity and the exact returned objects for the active lifecycle.
- Defined browser security boundaries: clients provide opaque identifiers, not
  filesystem paths, commands, environment variables, credentials, or arbitrary
  secrets.
- These additions are SDK contracts only. Sovereign remains responsible for
  runtime registries, authorization, and lifecycle; Workflows owns durable
  state; and feature packages own execution engines and console panels.

### Fixed

- Made direct `RunRecord` authorization return the same typed not-found error
  for tenant mismatch as tenant-scoped absence, while preserving explicit
  authorization errors for visible-record freshness, action, boundary, and
  capability denials.
- Made `RunPage` reject exact and divergent duplicate run IDs without silently
  changing its records, while preserving the 100-record stored-tuple bound.
- Restored runtime-resolvable return annotations on all Feature and HostFeature
  contribution methods without importing contribution models during base
  module import.

## 0.35.1 — 2026-08-09

### Fixed

- Made isolated-feature startup and shutdown retain authoritative ownership of
  child processes, process waits, client cleanup, event delivery, and hostile
  cancellation paths until they settle or are safely fenced for a retry.
- Ensured TERM/KILL escalation is bounded and one-time, stream disposal follows
  process observation on Windows, and unresolved retirement cannot admit a
  replacement or retain decoded event payloads and handler secrets.
- Hardened SDK CI and PyPI publishing with Python 3.11–3.14 Linux/Windows
  coverage, exact artifact tests, immutable tag/SHA validation, pinned actions
  and build tooling, and a final non-OIDC release revalidation gate.

## 0.35.0 — 2026-08-03

### Added

- Private inference providers now implement an owner-scoped, idempotent
  ``touch`` operation. Core calls it for real LLM traffic so scale-to-zero
  runtimes renew their idle deadline without provisioning replacement capacity
  or exposing route credentials.

### Changed

- **Breaking for inference-lease providers.** ``InferenceLeaseProvider`` is
  runtime-checkable, so a 0.34.0 provider that does not implement ``touch``
  now fails ``isinstance`` against the protocol and is rejected where
  conformance is checked, rather than failing later at first call. Providers
  published against 0.34.0 must add the operation to load under 0.35.0.
- **Behavior change for inference-lease providers.**
  ``InferenceLease.validate_for`` now also enforces an absolute lease
  lifetime: ``expires_at`` may not exceed
  ``requested_at + ready_deadline_seconds + expected_session_seconds``.
  Equality is allowed; anything beyond raises
  ``InferenceLeaseConstraintError``. Previously the validator placed no upper
  bound on ``expires_at``, so a provider that returned a coarse, natural
  expiry — an hourly-billed GPU host handing back ``created_at + 1h`` against
  the default 900s + 900s bounds, for example — validated under 0.34.0 and is
  rejected under 0.35.0. Providers must clamp the expiry they report to the
  window the request actually authorized. This closes an authorization gap
  where a lease could outlive the session its cost and readiness bounds were
  approved against; it does not change quoting, acquisition, or release.

## 0.34.0 — 2026-08-02

### Added

- A provider-neutral, owner-scoped remote inference lease contract for private
  on-demand model serving. The additive contract includes immutable request
  constraints, pre-provisioning quotes, explicit lifecycle states, sanitized
  failures and telemetry, and an in-memory route whose endpoint and credentials
  cannot enter agent-facing serialization.
- The `kestrel_sovereign.inference_lease_providers` entry-point group constant
  and runtime-checkable provider protocol for external infrastructure packages.
## 0.33.0 — 2026-07-26

### Added

- Capability-negotiated private host ingress for isolated features. Services
  register named, bounded JSON handlers; hosts use the typed
  `call_host_ingress()` API only after a compatible capability advertises the
  name. Ingress remains outside `tools/*`, is invisible to agent tool
  inventories, validates JSON at both trust boundaries, offloads sync handlers,
  and redacts errors and payload values.

## 0.32.0 — 2026-07-25

### Added

- A capability-negotiated, typed execution-context contract for isolated
  `tools/call` RPCs. Hosts can attach trusted invocation, idempotency, retry,
  and trigger metadata without exposing it in user-controlled tool arguments.
- Public `ToolExecutionContext`, `ToolExecutionTrigger`, related capability
  types, and `get_tool_execution_context()` for task-local handler access.
  Context is cleared after every invocation, including failed or cancelled
  calls, and context use fails closed with legacy services.
