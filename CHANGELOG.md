# Changelog

All notable changes to this project are documented in this file.

## 0.32.0 — 2026-07-25

### Added

- A capability-negotiated, typed execution-context contract for isolated
  `tools/call` RPCs. Hosts can attach trusted invocation, idempotency, retry,
  and trigger metadata without exposing it in user-controlled tool arguments.
- Public `ToolExecutionContext`, `ToolExecutionTrigger`, related capability
  types, and `get_tool_execution_context()` for task-local handler access.
  Context is cleared after every invocation, including failed or cancelled
  calls, and context use fails closed with legacy services.
