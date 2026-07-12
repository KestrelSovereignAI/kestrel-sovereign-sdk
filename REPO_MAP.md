# kestrel-sovereign-sdk — Repo Map

Auto-generated file-tree + per-file purpose index. Do **not** edit by hand —
regenerate via `python scripts/generate_repo_map.py` (refreshed nightly by
`.github/workflows/repo-map.yml`). No timestamp on purpose: the nightly job
commits only when the tree actually changes; `git log REPO_MAP.md` has the date.

**Scope:** 101 tracked files (90 `.py`, 4 `.md`, 7 other). Excludes caches, lockfiles, and build artifacts.

**Format per file:** `path — one-line purpose` plus the public top-level Python symbols on the next line
(classes and functions; private `_name` skipped).

---
## Top-level files

Repo entry points and standard project files.

- **README.md** — kestrel-sovereign-sdk — Lightweight SDK providing base interfaces, protocols, and utilities for Kestrel Sovereign feature package development.
- **AGENTS.md** — kestrel-sovereign-sdk — Agent Instructions — See [README.md](README.md) for package overview.
- **LICENSE** — —
- **.gitignore** — —
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
- **kestrel_sdk/features/base.py** — Base class for Kestrel Features — SDK interface.
  - `class TaskHandler`; `def parse_docstring_params(docstring)`; `class Feature`; `def tool(name, description, category, command_prefix)`
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
- **kestrel_sdk/isolated_feature/protocol.py** — Versioned JSON-RPC protocol for isolated feature runtimes.
  - `class ProtocolError`; `class ToolMetadata`; `class JsonRpcRequest`; `class JsonRpcNotification`; `class JsonRpcError`; `class JsonRpcResponse`; `def encode_message(message)`; `def decode_message(line)`
- **kestrel_sdk/isolated_feature/service.py** — Service-side base class for isolated feature runtimes.
  - `class IsolatedFeatureService`
- **kestrel_sdk/llm/__init__.py** — LLM-related types, protocols, and the adapter contract.
- **kestrel_sdk/llm/adapter.py** — LLM adapter abstract base — the contract every provider implements.
  - `class LLMAdapter`
- **kestrel_sdk/llm/capabilities.py** — Provider-level LLM capability metadata.
  - `class StructuredOutputMode`; `class ToolStreamingMode`; `class VisionInputMode`; `class ReasoningControlMode`; `class PromptCacheMode`; `class BatchMode`; `class FilesMode`; `class TokenCountMode`; `…`
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
- **kestrel_sdk/tools/result.py** — Tool result envelope contract.
  - `class ToolResultStatus`; `class ToolResult`
- **kestrel_sdk/tools/waitable.py** — Waitable provider contract.
  - `class Outcome`; `class WaitStatus`; `class Waitable`; `class MonitorableWaitable`
- **kestrel_sdk/training/__init__.py** — Training provider interfaces for Kestrel SDK.
- **kestrel_sdk/training/protocol.py** — TrainingProvider Protocol for unified LoRA training across providers.
  - `class TrainingProvider`; `class TrainingProviderError`; `class ProviderNotAvailableError`; `class TrainingSubmissionError`; `class TrainingStatusError`; `class DownloadError`; `class GenerationError`
- **kestrel_sdk/training/types.py** — Unified types for TrainingProvider protocol.
  - `class TrainingState`; `class ProviderType`; `class ProviderCapabilities`; `class TrainingConfig`; `class TrainingJob`; `class TrainingStatus`; `class GenerationState`; `class GenerationConfig`; `…`
- **kestrel_sdk/voice/__init__.py** — Kestrel SDK — Voice provider interfaces.
- **kestrel_sdk/voice/base.py** — Base classes for voice providers (TTS and STT).
  - `class VoiceInfo`; `class VoiceConfig`; `class PersonalityFingerprint`; `def match_voice(personality, available_voices)`; `def split_sentences(text)`; `class TTSProvider`; `class STTProvider`
- **kestrel_sdk/voice/conversation_base.py** — ConversationProvider — speech-to-speech provider contract.
  - `class AudioFormat`; `class TurnDetectionConfig`; `class ToolDef`; `class SessionCreatedEvent`; `class SessionUpdatedEvent`; `class SpeechStartedEvent`; `class SpeechStoppedEvent`; `class TranscriptDeltaEvent`; `…`

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
  - `def feature()`; `class TestToolResultPassThrough`; `class TestLegacyShapeDuringMigration`
- **tests/test_extension_contracts.py** — Contract tests shared by agent UI and application extensions.
  - `def test_agent_feature_ui_contract_is_sdk_owned()`; `def test_ui_contributions_preserves_0292_positional_order()`; `def test_app_extension_defaults_are_safe_noops()`
- **tests/test_hook_input.py** — SDK 0.9 — HookInput narration-check fields (kestrel-sovereign #1048 Wave 5D).
  - `def test_post_response_narration_fields_present_and_default_to_none()`; `def test_post_response_narration_fields_round_trip_through_to_dict()`; `def test_to_dict_exact_shape_for_post_response_event()`; `def test_positional_args_through_agent_spawn_keep_pre_0_9_meaning()`; `def test_pre_0_9_callers_still_construct_without_narration_fields()`
- **tests/test_host_feature_contract.py** — Tests for the host-scoped feature contract (issue #46).
  - `class ExampleHostFeature`; `def test_hostfeature_importable_from_sdk_top_level()`; `def test_hostfeature_is_abc_and_distinct_from_feature()`; `def test_hostfeature_has_no_agent_binding()`; `def test_declares_required_contract_methods()`; `def test_name_and_capability_slugs()`; `def test_base_defaults_are_thin()`; `def test_get_router_mounts_at_host_root()`; `…`
- **tests/test_isolated_feature.py** — Tests for the isolated feature JSON-RPC stdio contract.
  - `class MemoryReader`; `class MemoryWriter`; `def memory_stdio_pair()`; `async def test_service_client_lifecycle_tools_and_events()`; `async def test_unknown_tool_returns_json_rpc_error()`; `def test_protocol_rejects_non_object_tool_schema()`; `async def test_tools_are_gated_until_health_reports_ready()`; `def test_feature_event_notification_round_trip()`; `…`
- **tests/test_isolated_feature_concurrency.py** — A slow tool handler must not wedge the whole isolated-feature service.
  - `async def test_slow_tool_does_not_block_health_or_other_requests()`; `async def test_shutdown_terminates_even_with_a_stuck_handler()`
- **tests/test_isolated_feature_robustness.py** — Robustness regressions for the isolated-feature runtime (review Wave 1).
  - `async def test_blocking_sync_handler_does_not_wedge_health()`; `async def test_read_loop_death_fails_inflight_requests()`; `async def test_request_after_read_loop_death_fails_fast()`; `async def test_close_during_inflight_request_does_not_hang()`; `async def test_wrapper_reattaches_event_handlers_after_restart()`
- **tests/test_llm_contract.py** — Tests for the LLM provider contract.
  - `class TestContractVersion`; `class TestToolCall`; `class TestLLMResponse`; `class TestModelCategory`; `class TestModelInfo`; `class TestProviderInfo`; `class TestProviderCapabilities`; `class TestBackendType`; `…`
- **tests/test_payer_policy.py** — Tests for kestrel_sdk.payer_policy.
  - `class TestEnums`; `class TestSupportMatrix`; `class TestPayerSpec`; `class TestPayerPolicy`; `class TestTOMLRoundTrip`; `class TestResolvedResource`
- **tests/test_security_encryption.py** — Tests for SDK encryption key derivation and migration behavior.
  - `def key_shape(request, monkeypatch, tmp_path)`; `def clear_master_key_cache()`; `def test_passphrase_master_key_uses_salted_pbkdf2_600k(monkeypatch, tmp_path)`; `def test_env_passphrase_without_salt_warns_once_and_uses_legacy(monkeypatch, caplog, tmp_path)`; `def test_same_passphrase_and_env_salt_yield_same_key_across_derivations(monkeypatch)`; `def test_key_file_passphrase_persists_salt_next_to_key(monkeypatch, tmp_path)`; `def test_key_file_passphrase_salt_write_failure_falls_back_to_legacy(monkeypatch, caplog, tmp_path)`; `def test_passphrase_master_key_is_cached_for_encrypt_decrypt(monkeypatch)`; `…`
- **tests/test_timeline_protocol.py** — Tests for timeline protocols.
  - `class StubTimeline`; `class StubEvent`; `class StubPerson`; `def test_timeline_protocol_conformance()`; `def test_timeline_protocol_optional_subject_name()`; `def test_event_protocol_conformance()`; `def test_event_protocol_optional_fields()`; `def test_person_protocol_conformance()`; `…`
- **tests/test_timeline_sharing.py** — Tests for timeline sharing protocols.
  - `class StubTimeline`; `class StubEvent`; `class StubPerson`; `def test_json_serializer_is_protocol()`; `def test_json_serializer_content_type()`; `def test_json_serializer_produces_valid_json()`; `def test_json_serializer_round_trip()`; `def test_json_serializer_handles_null_fields()`; `…`
- **tests/test_tool_result.py** — Tests for ToolResult — the cross-feature tool envelope contract.
  - `class TestToolResultStatus`; `class TestToolResultOk`; `class TestToolResultFailed`; `class TestToolResultPartial`; `class TestToolResultTypeGuards`; `class TestToolResultFrozen`; `class TestToolResultSerialization`; `class TestHonestyLayerInvariants`
- **tests/test_tool_schema_annotations.py** — @tool parameter-schema generation from real and PEP 563 annotations.
  - `def test_resolve_optional_and_union_and_generics()`; `def test_resolved_flag_distinguishes_mapping_from_fallback()`; `def test_no_warning_for_optional_str_but_warning_for_true_fallback(caplog)`; `def test_tool_schema_under_pep563_string_annotations()`; `def test_unresolvable_annotation_falls_back_to_string_without_crashing()`; `def test_raw_str_annotation_after_hint_failure_resolves_without_warning(caplog)`
- **tests/test_vector_search_protocol.py** — Tests for vector search backend protocol.
  - `class StubVectorSearchBackend`; `async def test_vector_search_backend_conformance()`; `async def test_vector_search_backend_callable()`; `async def test_vector_search_backend_without_filter()`; `async def test_vector_search_backend_supports_filters_property()`; `async def test_vector_search_backend_returns_tuples()`
- **tests/test_waitable.py** — Tests for the Waitable provider contract.
  - `class TestOutcome`; `class TestWaitStatus`; `class TestWaitableProtocol`; `class TestMonitorableWaitable`
