# kestrel-sovereign-sdk — Repo Map

Auto-generated file-tree + per-file purpose index. Do **not** edit by hand —
regenerate via `python scripts/generate_repo_map.py` (refreshed nightly by
`.github/workflows/repo-map.yml`). No timestamp on purpose: the nightly job
commits only when the tree actually changes; `git log REPO_MAP.md` has the date.

**Scope:** 126 tracked files (114 `.py`, 5 `.md`, 7 other). Excludes caches, lockfiles, and build artifacts.

**Format per file:** `path — one-line purpose` plus the public top-level Python symbols on the next line
(classes and functions; private `_name` skipped).

---
## Top-level files

Repo entry points and standard project files.

- **README.md** — kestrel-sovereign-sdk — Lightweight SDK providing base interfaces, protocols, and utilities for Kestrel Sovereign feature package development.
- **AGENTS.md** — kestrel-sovereign-sdk — Agent Instructions — See [README.md](README.md) for package overview.
- **LICENSE** — —
- **.gitignore** — —
- **CHANGELOG.md** — Changelog — All notable changes to this project are documented in this file.
- **REPO_MAP.md** — kestrel-sovereign-sdk — Repo Map — Auto-generated file-tree + per-file purpose index.
- **pyproject.toml** — (configuration)

## `.github/`

- **.github/workflows/ci.yml** — (configuration)
- **.github/workflows/publish.yml** — (configuration)
- **.github/workflows/repo-map.yml** — (configuration)

## `docs/`

- **docs/code_reviews/claude-pr-24.md** — Claude Review: PR #24 — - PR: https://github.com/KestrelSovereignAI/kestrel-sovereign-sdk/pull/24 - Reviewed: 2026-05-26T16:42:09Z

## `kestrel_sdk/`

- **kestrel_sdk/__init__.py** — Kestrel Sovereign SDK — lightweight interfaces for feature packages.
- **kestrel_sdk/_validation.py** — Validation helpers shared by public SDK contract modules.
  - `def stable_token(value, field_name)`; `def non_empty_text(value, field_name)`; `def browser_safe_string(value, field_name)`; `def semantic_version(value, field_name)`; `def semantic_version_parts(value)`; `def frozen_tokens(values, field_name)`; `def unique_tuple(values, field_name)`
- **kestrel_sdk/a2a/__init__.py** — Kestrel SDK — A2A Protocol interfaces.
- **kestrel_sdk/a2a/agent_card.py** — Agent Card Types for A2A Protocol.
  - `class AgentProvider`; `class AgentCapabilities`; `class AgentAuthentication`; `class AgentSkill`; `class AgentCard`
- **kestrel_sdk/a2a/types.py** — A2A Protocol Types for Kestrel.
  - `class JSONRPCMessage`; `class JSONRPCRequest`; `class JSONRPCError`; `class JSONRPCResponse`; `class TaskState`; `class TextPart`; `class FileContent`; `class FilePart`; `…`
- **kestrel_sdk/channels/__init__.py** — Channel adapter contracts for messaging integrations.
- **kestrel_sdk/channels/base.py** — Abstract base class for channel adapter packages.
  - `class ChannelAdapter`
- **kestrel_sdk/channels/models.py** — Shared models for channel adapter packages.
  - `class MessageDirection`; `class DeliveryStatus`; `class ChannelMessage`; `class DeliveryReceipt`; `class ChannelConfig`
- **kestrel_sdk/config/__init__.py** — Kestrel SDK — Configuration constants and defaults.
- **kestrel_sdk/config/constants.py** — Kestrel Configuration Constants.
  - `class Currency`
- **kestrel_sdk/config/defaults.py** — Default configuration values for Kestrel.
  - `def get_ollama_url()`; `def get_ipfs_api_url()`; `def get_mcp_gateway_url()`; `def get_lotus_rpc_url()`; `def get_lighthouse_api_url()`; `def get_openrouter_api_base()`; `def get_lighthouse_gateway_url()`; `def get_storacha_gateway_url()`; `…`
- **kestrel_sdk/delivery/__init__.py** — Delivery provider contracts for outbound integrations.
- **kestrel_sdk/delivery/base.py** — Provider protocol for outbound delivery packages.
  - `class DeliveryProvider`
- **kestrel_sdk/delivery/models.py** — Shared models for durable outbound delivery providers.
  - `class DeliveryTask`; `class DeliveryResult`
- **kestrel_sdk/deploy/__init__.py** — Kestrel SDK — Deployment provider interfaces.
- **kestrel_sdk/deploy/base.py** — Deploy Provider Abstract Base Class.
  - `class DeployProvider`
- **kestrel_sdk/deploy/models.py** — Deploy Data Models and Exceptions.
  - `class DeployStatus`; `class DeployProviderType`; `class DeploymentProfile`; `class DeploymentSession`; `class DeployManagerError`
- **kestrel_sdk/extensions/__init__.py** — Application-level extension contracts.
- **kestrel_sdk/extensions/app_extension.py** — Application-specific agent extension contract.
  - `class AppExtension`
- **kestrel_sdk/features/__init__.py** — Kestrel SDK — Feature interfaces.
- **kestrel_sdk/features/_contribution_support.py** — Lightweight helpers shared by the feature base contracts.
  - `def contribution_annotation(name)`; `def implementation_contribution_owner(implementation)`
- **kestrel_sdk/features/base.py** — Base class for Kestrel Features — SDK interface.
  - `class TaskHandler`; `def parse_docstring_params(docstring)`; `class Feature`; `def tool(name, description, category, command_prefix)`
- **kestrel_sdk/features/contributions.py** — Declarative feature contribution contracts.
  - `class PermissionLevel`; `class FeaturePermissionDefaults`; `class ContributionContractError`; `class WaitProviderRegistration`; `class WorkflowRegistration`; `class ContextClauseRegistration`; `class SetupStepContext`; `class SetupFlow`; `…`
- **kestrel_sdk/features/host_base.py** — Host-scoped feature contract — SDK interface.
  - `class HostContext`; `class HostFeature`
- **kestrel_sdk/features/ui.py** — SDK-owned UI contribution shape.
  - `class UIContributions`
- **kestrel_sdk/hooks/__init__.py** — Kestrel SDK — Hook interfaces.
- **kestrel_sdk/hooks/base.py** — Kestrel Hooks - Core Types (Claude Code Aligned).
  - `class HookEvent`; `class PermissionDecision`; `class HookInput`; `class HookOutput`; `class Hook`
- **kestrel_sdk/isolated_feature/__init__.py** — Isolated feature stdio JSON-RPC runtime contract.
- **kestrel_sdk/isolated_feature/client.py** — Host-side client for isolated feature stdio JSON-RPC runtimes.
  - `class IsolatedFeatureClient`; `class SubprocessIsolatedFeatureClient`
- **kestrel_sdk/isolated_feature/context.py** — Task-local access to trusted isolated-tool execution metadata.
  - `def get_tool_execution_context()`
- **kestrel_sdk/isolated_feature/protocol.py** — Versioned JSON-RPC protocol for isolated feature runtimes.
  - `class ProtocolError`; `class ConfigTransitionError`; `class ConfigTransitionUnsupportedError`; `class ToolExecutionContextUnsupportedError`; `class HostIngressError`; `class HostIngressUnsupportedError`; `class HostIngressUnknownNameError`; `def validate_host_ingress_name(value)`; `…`
- **kestrel_sdk/isolated_feature/service.py** — Service-side base class for isolated feature runtimes.
  - `class IsolatedFeatureService`
- **kestrel_sdk/llm/__init__.py** — LLM-related types, protocols, and the adapter contract.
- **kestrel_sdk/llm/adapter.py** — LLM adapter abstract base — the contract every provider implements.
  - `class LLMAdapter`
- **kestrel_sdk/llm/capabilities.py** — Provider-level LLM capability metadata.
  - `class StructuredOutputMode`; `class ToolStreamingMode`; `class VisionInputMode`; `class ReasoningControlMode`; `class PromptCacheMode`; `class BatchMode`; `class FilesMode`; `class TokenCountMode`; `…`
- **kestrel_sdk/llm/inference_lease.py** — Provider-neutral contracts for privately leased inference capacity.
  - `class InferenceLeaseState`; `class InferencePrivacy`; `class InferenceLeaseError`; `class InferenceLeaseConstraintError`; `class InferenceLeaseNotFoundError`; `class InferenceLeaseOwnershipError`; `class InferenceLeaseProviderUnavailableError`; `class InferenceLeaseProvisioningError`; `…`
- **kestrel_sdk/llm/model_info.py** — Standardized model metadata.
  - `class ModelCategory`; `class ModelInfo`
- **kestrel_sdk/llm/provider.py** — Provider/route registration record.
  - `class ProviderInfo`
- **kestrel_sdk/llm/response.py** — LLM response envelope — what every adapter returns.
  - `class ToolCall`; `class ToolCallStarted`; `class TokenCount`; `class FileRef`; `class CacheMarker`; `class WebSearchOptions`; `class CodeExecOptions`; `class ComputerUseOptions`; `…`
- **kestrel_sdk/llm/types.py** — Stable LLM-related types shared across feature packages.
  - `class BackendType`
- **kestrel_sdk/metrics.py** — Kestrel Prometheus Metrics — shared metric definitions for the Kestrel ecosystem.
  - `def generate_metrics()`; `def get_content_type()`
- **kestrel_sdk/operator/__init__.py** — Contracts for feature-owned, authorized operator execution.
- **kestrel_sdk/operator/context.py** — Authenticated, fail-closed context for operator execution.
  - `class OperatorAuthorizationError`; `class OperatorContext`
- **kestrel_sdk/operator/discovery.py** — Versioned service discovery contracts for feature-owned operators.
  - `class ServiceScope`; `class CapabilityDescriptor`; `class ServiceDescriptor`; `class ServiceReference`; `class ServiceRequirement`; `class ServiceRegistration`; `class ServiceResolver`
- **kestrel_sdk/operator/runs.py** — Immutable contracts for the feature-owned operator run plane.
  - `class RunConflictError`; `class RunNotFoundError`; `class RunSource`; `class RunState`; `class RunControlAction`; `class ArtifactAuthorizationAction`; `class RunLaunch`; `class RunRecord`; `…`
- **kestrel_sdk/operator/targets.py** — Browser-safe execution-target discovery and resolution contracts.
  - `class ExecutionTargetDescriptor`; `class ExecutionTargetReference`; `class ExecutionTargetResolver`
- **kestrel_sdk/outputs/__init__.py** — Output event contracts shared by workflows, channels, and delivery.
- **kestrel_sdk/outputs/models.py** — Provider-neutral output event envelopes.
  - `class OutputKind`; `class OutputDestination`; `class OutputEvent`
- **kestrel_sdk/payer_policy.py** — PayerPolicy — declarative model of who pays for which metered resource.
  - `class ResourceClass`; `class PayerKind`; `class SupportStatus`; `def status_for(resource_class, vendor, kind)`; `def is_offerable(resource_class, vendor, kind)`; `def supported_kinds_for(resource_class, vendor)`; `class KeyResolverProtocol`; `class ResolvedResource`; `…`
- **kestrel_sdk/py.typed** — —
- **kestrel_sdk/security/__init__.py** — Kestrel SDK — Security interfaces and encryption helpers.
- **kestrel_sdk/security/aead.py** — AEAD container — versioned AES-256-GCM with Fernet-compatible read path.
  - `class AEADCipher`
- **kestrel_sdk/security/encryption.py** — Symmetric encryption helpers for Kestrel SDK.
  - `def get_fernet()`; `def get_master_key_bytes()`; `def get_agent_fernet(agent_id)`; `def get_agent_key(agent_did, purpose)`; `def encrypt(agent_did, purpose, plaintext)`; `def decrypt(agent_did, purpose, ciphertext)`; `def encrypt_string(agent_did, purpose, plaintext)`; `def decrypt_string(agent_did, purpose, ciphertext)`; `…`
- **kestrel_sdk/security/exceptions.py** — Unified exception hierarchy for Kestrel security module.
  - `class SecurityError`; `class KeyStorageError`; `class KeyNotFoundError`; `class KeyNotConfiguredError`; `class EncryptionError`; `class DecryptionError`; `class MasterKeyNotConfiguredError`; `class InvalidPurposeError`; `…`
- **kestrel_sdk/signals/__init__.py** — Kestrel SDK — Signal Dispatcher interfaces.
- **kestrel_sdk/signals/models.py** — Signal Dispatcher data model — public contract.
  - `class SignalMode`; `class Trust`; `class Urgency`; `class Visibility`; `class Status`; `class ResourceLock`; `class CausationFrame`; `class RateLimit`; `…`
- **kestrel_sdk/storage/__init__.py** — Kestrel SDK — Storage provider interfaces.
- **kestrel_sdk/storage/database/__init__.py** — Kestrel SDK — relational database surface for feature packages.
- **kestrel_sdk/storage/database/interface.py** — Database backend ABC.
  - `class DatabaseBackend`; `class DatabaseError`; `class ConnectionError`; `class QueryError`; `class TransactionError`
- **kestrel_sdk/storage/database/privacy.py** — Privacy → engine target mapping for feature ORM packages.
  - `class PrivacyMode`; `class EngineTarget`; `def resolve_engine_target(mode, fallback_url)`
- **kestrel_sdk/storage/providers/__init__.py** — Kestrel SDK — Storage provider interfaces.
- **kestrel_sdk/storage/providers/base.py** — Storage Provider Protocol
  - `class StorageTier`; `class SyncStatus`; `class StorageResult`; `class SyncItem`; `class SyncManifest`; `class StorageProvider`; `class CryostasisCapable`; `class MultiCurrencyPayment`
- **kestrel_sdk/testing/__init__.py** — Plugin conformance helpers (SDK 0.8.0+).
- **kestrel_sdk/testing/conformance.py** — Conformance assertions for the LLM adapter streaming contract.
  - `class StreamingWithToolsResult`; `async def drain_streaming_with_tools(stream)`; `async def drain_streaming_text_only(stream)`; `def assert_tool_call_started_contract(marker)`; `def assert_response_contract(response)`
- **kestrel_sdk/timeline/__init__.py** — Timeline protocols for cross-implementation interop.
- **kestrel_sdk/timeline/protocol.py** — Timeline Protocol — minimal duck-typed shape for timeline implementations.
  - `class TimelineProtocol`; `class EventProtocol`; `class PersonProtocol`
- **kestrel_sdk/timeline/sharing.py** — Timeline sharing protocols for cross-system serialization.
  - `class TimelineSharingProtocol`; `class JSONTimelineSerializer`
- **kestrel_sdk/timeline/vector_search.py** — Vector search backend protocol for semantic timeline search.
  - `class VectorSearchBackend`
- **kestrel_sdk/tools/__init__.py** — Kestrel SDK — Tool interfaces.
- **kestrel_sdk/tools/base.py** — Base classes and interfaces for Kestrel agent tools.
  - `class ToolCategory`; `class ToolParameter`; `class ToolSchema`; `class AgentTool`; `class ToolExecutionError`
- **kestrel_sdk/tools/parts.py** — Pending typed-parts buffer for the tool call currently executing.
  - `def tool_result_parts_buffer()`; `def current_tool_result_parts()`
- **kestrel_sdk/tools/result.py** — Tool result envelope contract.
  - `class ToolResultStatus`; `class ToolResult`
- **kestrel_sdk/tools/waitable.py** — Waitable provider contract.
  - `class Outcome`; `class WaitStatus`; `class Waitable`; `class MonitorableWaitable`
- **kestrel_sdk/tracing.py** — Kestrel OTel instrumentation helper — OpenInference span builders + OTLP export.
  - `class KestrelTracer`; `def configure()`
- **kestrel_sdk/training/__init__.py** — Training provider interfaces for Kestrel SDK.
- **kestrel_sdk/training/protocol.py** — TrainingProvider Protocol for unified LoRA training across providers.
  - `class TrainingProvider`; `class TrainingProviderError`; `class ProviderNotAvailableError`; `class TrainingSubmissionError`; `class TrainingStatusError`; `class DownloadError`; `class GenerationError`
- **kestrel_sdk/training/types.py** — Unified types for TrainingProvider protocol.
  - `class TrainingState`; `class ProviderType`; `class ProviderCapabilities`; `class TrainingConfig`; `class TrainingJob`; `class TrainingStatus`; `class GenerationState`; `class GenerationConfig`; `…`
- **kestrel_sdk/voice/__init__.py** — Kestrel SDK — Voice provider interfaces.
- **kestrel_sdk/voice/base.py** — Base classes for voice providers (TTS and STT).
  - `class VoiceInfo`; `class VoiceConfig`; `class PersonalityFingerprint`; `def match_voice(personality, available_voices)`; `def split_sentences(text)`; `class TTSProvider`; `class STTProvider`
- **kestrel_sdk/voice/conversation_base.py** — ConversationProvider — speech-to-speech provider contract.
  - `class AudioFormat`; `class TurnDetectionConfig`; `class ToolDef`; `class RealtimeTransport`; `class ConversationCapabilities`; `class EphemeralClientSecret`; `class RealtimeClientSession`; `class VoiceToolCall`; `…`

## `scripts/`

- **scripts/generate_repo_map.py** — Generate REPO_MAP.md — a file-tree + per-file purpose index for this repo.
  - `class FileEntry`; `def repo_name()`; `def tracked_files()`; `def is_excluded(path)`; `def first_sentence(text, max_chars)`; `def summarize_python(path)`; `def summarize_markdown(path)`; `def summarize_other(path)`; `…`

## `tests/`

- **tests/__init__.py** — —
- **tests/test_channel_delivery_contracts.py** — —
  - `class EchoChannel`; `class EchoDeliveryProvider`; `def test_async_channel_contract()`; `def test_async_delivery_contract()`; `def test_output_event_envelope_serializes_destination()`; `def test_entry_point_group_constants_are_stable()`
- **tests/test_conformance.py** — Tests for kestrel_sdk.testing — the plugin-author conformance helpers.
  - `class TestDrainStreamingWithToolsHappy`; `class TestDrainStreamingWithToolsViolations`; `class TestDrainStreamingTextOnly`; `class TestAssertToolCallStartedContract`; `class TestAssertResponseContract`
- **tests/test_constitution_injection_fields.py** — SDK Phase 1.1A — SourceRegistration constitutional-injection fields.
  - `def test_require_constitution_echo_defaults_to_false()`; `def test_prompt_template_format_defaults_to_claude_code()`; `def test_constitution_injection_defaults_to_none()`; `def test_system_prompt_budget_bytes_defaults_to_none()`; `def test_pre_0_11_caller_constructs_cleanly()`; `def test_codex_reviewer_can_set_require_echo_true_at_sdk_level()`; `def test_local_reviewer_format_settable()`; `def test_bare_format_settable_for_caller_responsibility()`; `…`
- **tests/test_database_surface.py** — Tests for kestrel_sdk.storage.database surface (issue #1094).
  - `def test_module_exports_match_acceptance_criteria()`; `def test_database_backend_is_abstract()`; `def test_error_hierarchy()`; `def test_privacy_mode_string_round_trip()`; `def test_privacy_mode_hashes_identical_to_string()`; `def test_resolve_ephemeral_ignores_fallback()`; `def test_resolve_isolated_creates_tempfile_and_is_volatile()`; `def test_isolated_cleanup_is_idempotent()`; `…`
- **tests/test_docstring_parser.py** — Regression tests for parse_docstring_params wrapped-description truncation.
  - `def test_wrapped_description_not_truncated_at_word_continuation()`; `def test_single_line_params_unchanged()`; `def test_param_with_type_annotation()`
- **tests/test_dynamic_tool_result.py** — Tests for ``DynamicTool.execute``'s ToolResult-aware pass-through.
  - `def feature()`; `class TestToolResultPassThrough`; `class TestLegacyShapeDuringMigration`; `def parts_feature()`; `class TestEnvelopeParts`
- **tests/test_extension_contracts.py** — Contract tests shared by agent UI and application extensions.
  - `def test_agent_feature_ui_contract_is_sdk_owned()`; `def test_ui_contributions_preserves_0292_positional_order()`; `def test_app_extension_defaults_are_safe_noops()`
- **tests/test_feature_contribution_contracts.py** — Contract tests for external feature-owned declarative contributions.
  - `class ExternalFixtureFeature`; `def test_external_feature_can_expose_every_row_one_seam_via_sdk()`; `def test_contribution_methods_return_instance_stable_objects()`; `def test_owned_identity_supports_exact_deterministic_teardown()`; `def test_permission_vocabulary_is_closed_conservative_and_immutable()`; `def test_registration_validation_prevents_ambiguous_identity()`; `def test_workflow_registration_supports_empty_and_plural_sources()`; `def test_setup_flow_normalizes_real_non_string_enum()`; `…`
- **tests/test_hook_input.py** — SDK 0.9 — HookInput narration-check fields (kestrel-sovereign #1048 Wave 5D).
  - `def test_post_response_narration_fields_present_and_default_to_none()`; `def test_post_response_narration_fields_round_trip_through_to_dict()`; `def test_to_dict_exact_shape_for_post_response_event()`; `def test_positional_args_through_agent_spawn_keep_pre_0_9_meaning()`; `def test_pre_0_9_callers_still_construct_without_narration_fields()`
- **tests/test_host_feature_contract.py** — Tests for the host-scoped feature contract (issue #46).
  - `class ExampleHostFeature`; `def test_hostfeature_importable_from_sdk_top_level()`; `def test_hostfeature_is_abc_and_distinct_from_feature()`; `def test_hostfeature_has_no_agent_binding()`; `def test_declares_required_contract_methods()`; `def test_name_and_capability_slugs()`; `def test_base_defaults_are_thin()`; `async def test_host_contribution_owner_does_not_claim_legacy_owner(legacy_owner)`; `…`
- **tests/test_inference_lease_contract.py** — Contract tests for provider-neutral remote inference leases.
  - `def make_request()`; `def make_quote()`; `def make_route()`; `def make_lease()`; `def test_entry_point_group_is_stable()`; `def test_request_is_normalized_deeply_immutable_and_public_owner_free()`; `def test_request_rejects_malformed_or_unbounded_values(overrides, message)`; `def test_public_metadata_rejects_secret_like_keys(metadata)`; `…`
- **tests/test_isolated_feature.py** — Tests for the isolated feature JSON-RPC stdio contract.
  - `def test_service_rejects_non_boolean_inbound_producer_declaration(invalid)`; `class MemoryReader`; `class MemoryWriter`; `def memory_stdio_pair()`; `async def test_inbound_producer_declaration_crosses_initialize_boundary(has_producer)`; `def test_inbound_producer_accessor_fails_resident_for_malformed_metadata(hostile)`; `def test_subprocess_wrapper_agrees_with_direct_client(has_producer)`; `def test_subprocess_wrapper_fails_resident_for_hostile_metadata()`; `…`
- **tests/test_isolated_feature_concurrency.py** — A slow tool handler must not wedge the whole isolated-feature service.
  - `async def test_slow_tool_does_not_block_health_or_other_requests()`; `async def test_shutdown_terminates_even_with_a_stuck_handler()`
- **tests/test_isolated_feature_config_transition.py** — Negotiated host config-transition lifecycle coverage.
  - `async def test_legacy_service_does_not_advertise_or_receive_transition_requests()`; `async def test_transition_round_trip_receives_next_config_with_old_config_intact()`; `async def test_live_apply_requires_capability_and_commits_config_after_hook()`; `async def test_transition_failure_is_returned_without_reflecting_secrets(caplog)`; `async def test_transition_shutdown_and_health_are_deterministically_serialized()`; `async def test_timed_out_transition_discards_late_response_and_requires_replacement()`; `async def test_restart_required_rejects_repeated_transitions_client_and_service_side()`; `async def test_wrapper_retains_first_replacement_config_after_later_rejection()`; `…`
- **tests/test_isolated_feature_execution_context.py** — Execution-context propagation and isolation for isolated tool RPCs.
  - `async def test_execution_context_round_trip_does_not_mutate_tool_arguments()`; `async def test_retry_preserves_idempotency_key_and_changes_attempt()`; `async def test_concurrent_calls_cannot_observe_each_others_context()`; `async def test_async_and_sync_handlers_receive_the_same_execution_context()`; `async def test_failure_and_cancelled_wait_leave_no_execution_context_behind()`; `async def test_background_task_loses_execution_context_after_rpc_returns()`; `async def test_cancelled_sync_worker_loses_execution_context_after_rpc_cancellation()`; `async def test_legacy_calls_work_in_both_directions_and_context_fails_closed()`; `…`
- **tests/test_isolated_feature_host_ingress.py** — Private, capability-negotiated host-ingress contract coverage.
  - `def test_host_ingress_protocol_capability_and_boundary_validation()`; `async def test_non_ascii_host_ingress_names_fail_closed(name)`; `async def test_registered_ingress_is_capability_negotiated_and_tool_invisible()`; `async def test_sync_and_async_host_ingress_handlers_are_supported()`; `async def test_legacy_malformed_and_unknown_ingress_capabilities_fail_closed()`; `async def test_client_and_service_reject_malformed_or_oversized_payloads_safely()`; `async def test_host_ingress_errors_do_not_reflect_handler_or_payload_secrets(caplog)`; `async def test_cancelled_host_ingress_child_returns_generic_error_without_hanging(implementation)`; `…`
- **tests/test_isolated_feature_robustness.py** — Robustness regressions for the isolated-feature runtime (review Wave 1).
  - `async def test_blocking_sync_handler_does_not_wedge_health()`; `async def test_read_loop_death_fails_inflight_requests()`; `async def test_request_after_read_loop_death_fails_fast()`; `async def test_close_during_inflight_request_does_not_hang()`; `async def test_wrapper_reattaches_event_handlers_after_restart()`
- **tests/test_isolated_feature_stdio.py** — Adversarial regression tests for the isolated-feature stdio adapters.
  - `async def test_cancelled_send_keeps_windows_frames_ordered_and_lossless()`; `async def test_windows_writer_close_waits_for_active_write_without_blocking()`; `async def test_windows_writer_handles_partial_writes_and_reports_terminal_failures()`; `async def test_windows_writer_partial_frame_failure_fences_queued_and_later_drains()`; `async def test_windows_reader_cancellation_never_uses_default_executor(monkeypatch)`; `async def test_windows_reader_cancelled_asyncio_run_exits_without_waiting_for_worker()`; `async def test_windows_writer_success_releases_references_while_open_and_idle()`; `async def test_windows_writer_terminal_failure_releases_queued_references()`; `…`
- **tests/test_isolated_feature_stop.py** — Bounded, cancellation-safe subprocess retirement regressions (issue #66).
  - `async def test_cancelled_stop_retains_detached_child_and_delivers_first_cancellation(monkeypatch)`; `async def test_stop_never_reaped_process_uses_only_signal_phase_observations(monkeypatch)`; `async def test_stop_does_not_reobserve_process_after_prior_close_task(monkeypatch)`; `async def test_stop_reobserves_process_after_client_close_can_unblock_waiter(monkeypatch)`; `async def test_stop_timeout_does_not_fence_start_that_settles_before_timeout_update(monkeypatch)`; `async def test_stop_preserves_nested_timeout_cancellation_counts(monkeypatch)`; `async def test_stop_nested_timeout_and_external_cancels_continue_after_catch(monkeypatch)`; `async def test_start_rejects_while_a_detached_child_has_unresolved_retirement(monkeypatch)`; `…`
- **tests/test_llm_contract.py** — Tests for the LLM provider contract.
  - `class TestContractVersion`; `class TestToolCall`; `class TestLLMResponse`; `class TestModelCategory`; `class TestModelInfo`; `class TestProviderInfo`; `class TestProviderCapabilities`; `class TestBackendType`; `…`
- **tests/test_operator_contracts.py** — Focused tests for feature-owned operator SDK contracts.
  - `def test_operator_context_rejects_invalid_or_missing_tenant(tenant_id)`; `def test_operator_context_requires_tenant_field()`; `def test_operator_context_normalizes_collections_and_is_frozen()`; `def test_operator_context_helpers_fail_closed_for_unknown_values()`; `def test_operator_context_has_bounded_freshness_and_agent_attribution()`; `def test_service_descriptors_are_versioned_and_scope_is_explicit()`; `def test_stable_service_requirement_uses_compatible_major_and_minimum()`; `def test_service_requirement_rejects_non_stable_versions(version)`; `…`
- **tests/test_payer_policy.py** — Tests for kestrel_sdk.payer_policy.
  - `class TestEnums`; `class TestSupportMatrix`; `class TestPayerSpec`; `class TestPayerPolicy`; `class TestTOMLRoundTrip`; `class TestResolvedResource`
- **tests/test_public_contract_exports.py** — Public import and compatibility guarantees for row-1 SDK contracts.
  - `def test_operator_package_exports_complete_contract_surface()`; `def test_top_level_selectively_reexports_canonical_operator_contracts()`; `def test_feature_package_exports_contribution_contracts()`; `def test_run_contract_surface_has_one_canonical_name_per_model()`; `def test_sdk_contract_imports_have_no_runtime_framework_dependency()`; `def test_feature_base_modules_do_not_runtime_import_contribution_models()`; `def test_historic_base_module_ui_annotation_resolves_at_runtime()`; `def test_all_contribution_method_annotations_resolve_without_extra_globals()`; `…`
- **tests/test_run_contracts.py** — Focused tests for durable operator run-plane contracts.
  - `def test_launch_contains_only_semantic_input_and_record_owns_runtime_clock()`; `def test_launch_authorization_binds_all_trusted_authority()`; `def test_manual_and_agent_launch_shape_invariants()`; `def test_run_models_are_frozen_and_correlate_attempt_and_external_job()`; `def test_resolved_run_authorization_rechecks_durable_launch_scope()`; `def test_resolved_run_tenant_mismatch_matches_tenant_scoped_absence()`; `def test_terminal_state_and_separate_control_and_artifact_actions()`; `def test_control_expected_sequence_is_validated_and_outside_key_scope()`; `…`
- **tests/test_security_encryption.py** — Tests for SDK encryption key derivation and migration behavior.
  - `def key_shape(request, monkeypatch, tmp_path)`; `def clear_master_key_cache()`; `def test_passphrase_master_key_uses_salted_pbkdf2_600k(monkeypatch, tmp_path)`; `def test_env_passphrase_without_salt_warns_once_and_uses_legacy(monkeypatch, caplog, tmp_path)`; `def test_same_passphrase_and_env_salt_yield_same_key_across_derivations(monkeypatch)`; `def test_key_file_passphrase_persists_salt_next_to_key(monkeypatch, tmp_path)`; `def test_key_file_passphrase_salt_write_failure_falls_back_to_legacy(monkeypatch, caplog, tmp_path)`; `def test_passphrase_master_key_is_cached_for_encrypt_decrypt(monkeypatch)`; `…`
- **tests/test_timeline_protocol.py** — Tests for timeline protocols.
  - `class StubTimeline`; `class StubEvent`; `class StubPerson`; `def test_timeline_protocol_conformance()`; `def test_timeline_protocol_optional_subject_name()`; `def test_event_protocol_conformance()`; `def test_event_protocol_optional_fields()`; `def test_person_protocol_conformance()`; `…`
- **tests/test_timeline_sharing.py** — Tests for timeline sharing protocols.
  - `class StubTimeline`; `class StubEvent`; `class StubPerson`; `def test_json_serializer_is_protocol()`; `def test_json_serializer_content_type()`; `def test_json_serializer_produces_valid_json()`; `def test_json_serializer_round_trip()`; `def test_json_serializer_handles_null_fields()`; `…`
- **tests/test_tool_result.py** — Tests for ToolResult — the cross-feature tool envelope contract.
  - `class TestToolResultStatus`; `class TestToolResultOk`; `class TestToolResultFailed`; `class TestToolResultPartial`; `class TestToolResultTypeGuards`; `class TestToolResultFrozen`; `class TestToolResultSerialization`; `class TestHonestyLayerInvariants`; `…`
- **tests/test_tool_schema_annotations.py** — @tool parameter-schema generation from real and PEP 563 annotations.
  - `def test_resolve_optional_and_union_and_generics()`; `def test_resolved_flag_distinguishes_mapping_from_fallback()`; `def test_no_warning_for_optional_str_but_warning_for_true_fallback(caplog)`; `def test_tool_schema_under_pep563_string_annotations()`; `def test_unresolvable_annotation_falls_back_to_string_without_crashing()`; `def test_raw_str_annotation_after_hint_failure_resolves_without_warning(caplog)`
- **tests/test_tracing.py** — Tests for the OTel instrumentation helper (``kestrel_sdk.tracing``).
  - `class TestWorksWithoutExtra`; `class TestNoOpWhenUnconfigured`; `class TestConfigureEnabled`; `class TestSpanTree`; `class TestAttributeResolution`; `class TestLLMSpan`; `class TestProjectNameResourceAttr`; `class TestConventionDrift`
- **tests/test_vector_search_protocol.py** — Tests for vector search backend protocol.
  - `class StubVectorSearchBackend`; `async def test_vector_search_backend_conformance()`; `async def test_vector_search_backend_callable()`; `async def test_vector_search_backend_without_filter()`; `async def test_vector_search_backend_supports_filters_property()`; `async def test_vector_search_backend_returns_tuples()`
- **tests/test_voice_provider_contract.py** — —
  - `async def test_single_tool_providers_remain_batch_compatible()`; `async def test_parallel_provider_can_commit_one_batch()`; `def test_conversation_capabilities_serialize_specialized_tools()`; `def test_legacy_provider_gets_conservative_capabilities()`; `def test_provider_scoped_voices_do_not_leak_between_catalogs()`; `def test_provider_scoped_voice_keys_are_normalized()`; `def test_transcript_updates_can_declare_cumulative_corrections()`; `def test_legacy_tts_provider_gets_format_capabilities()`; `…`
- **tests/test_waitable.py** — Tests for the Waitable provider contract.
  - `class TestOutcome`; `class TestWaitStatus`; `class TestWaitableProtocol`; `class TestMonitorableWaitable`
