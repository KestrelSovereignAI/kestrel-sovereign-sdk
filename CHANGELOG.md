# Changelog

All notable changes to this project are documented in this file.

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
