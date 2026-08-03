# Changelog

All notable changes to this project are documented in this file.

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
