"""LLM-related types, protocols, and the adapter contract.

Exports the lightweight, stable surface that third-party LLM provider
plugins (and feature packages) need without depending on the full
``kestrel-sovereign`` framework. Heavy implementation — image
processing, retry envelopes, prompt-cache breadcrumbing, the provider
registry itself — stays in ``kestrel-sovereign``.

A third-party provider plugin imports from here, subclasses
:class:`LLMAdapter`, and registers under the
``kestrel_sovereign.llm_providers`` entry-point group::

    # in your_provider/adapter.py
    from kestrel_sdk.llm import LLMAdapter, LLMResponse, ModelInfo

    class KimiAdapter(LLMAdapter):
        async def get_response(self, client, model, messages, ...):
            ...
            return LLMResponse(content=text, ...)

    # in your pyproject.toml
    [project.entry-points."kestrel_sovereign.llm_providers"]
    kimi = "your_provider.adapter:KimiAdapter"

The framework picks the adapter up at startup and the agent gets a
new vendor without any edits to ``kestrel-sovereign``.

Contract version
----------------

:data:`SDK_LLM_CONTRACT_VERSION` is bumped when the abstract surface
or wire shape of the dataclasses changes in a way that breaks
existing plugins. Plugins that pin a major contract version can
detect drift at import time::

    from kestrel_sdk.llm import SDK_LLM_CONTRACT_VERSION
    assert SDK_LLM_CONTRACT_VERSION >= 1, "kestrel-sovereign-sdk too old"
"""

from .adapter import LLMAdapter
from .capabilities import (
    BatchMode,
    FilesMode,
    PromptCacheMode,
    ProviderCapabilities,
    ReasoningControlMode,
    ServerToolMode,
    StructuredOutputMode,
    TokenCountMode,
    ToolStreamingMode,
    VisionInputMode,
)
from .inference_lease import (
    INFERENCE_LEASE_PROVIDER_ENTRY_POINT_GROUP,
    InferenceLease,
    InferenceLeaseConstraintError,
    InferenceLeaseError,
    InferenceLeaseFailure,
    InferenceLeaseNotFoundError,
    InferenceLeaseOwnershipError,
    InferenceLeaseProvider,
    InferenceLeaseProviderUnavailableError,
    InferenceLeaseProvisioningError,
    InferenceLeaseQuote,
    InferenceLeaseRequest,
    InferenceLeaseState,
    InferencePrivacy,
    InferenceProviderCapability,
    InferenceRoute,
)
from .model_info import ModelCategory, ModelInfo
from .provider import ProviderInfo
from .response import (
    BatchHandle,
    BatchRequest,
    BatchResult,
    BatchStatus,
    CacheMarker,
    Citation,
    CodeExecOptions,
    ComputerUseOptions,
    FileRef,
    LLMResponse,
    MCPConnector,
    RawResponse,
    RequestOptions,
    ServerToolUse,
    TokenCount,
    ToolCall,
    ToolCallStarted,
    WebSearchOptions,
)
from .types import BackendType

# Bumped when the abstract surface of LLMAdapter, the wire shape of
# LLMResponse / ModelInfo / ProviderInfo, or the meaning of the
# ``kestrel_sovereign.llm_providers`` entry-point contract changes in
# a way that requires plugin authors to update their code.
#
# Version 3 (SDK 0.17.0): adds ProviderCapabilities, ProviderInfo.capabilities,
# and LLMAdapter.provider_capabilities() so provider feature metadata lives in
# the shared SDK contract.
#
# Version 4 (SDK 0.18.0): adds optional provider-owned embeddings via
# LLMAdapter.aembed()/aembed_batch() and ProviderCapabilities embedding
# metadata.
#
# Version 5: adds optional provider-surface negotiation for token counting,
# batch, files, prompt-cache shaping, reasoning controls, server tools,
# citations, raw passthrough, and neutral request/response dataclasses. This
# is additive; adapters pinning >=4 continue to work because get_response
# remains the only abstract method.
#
# Version 6 (SDK 0.35.0): tightens the inference-lease provider contract in
# two ways. Ordinary LLM adapters are unaffected by both; inference-capacity
# plugins must satisfy both to be v6-conformant.
#   (a) Providers must implement the owner-scoped, idempotent ``touch``
#       operation for idle-deadline renewal. This is structurally enforced —
#       the runtime-checkable protocol rejects a provider that omits it.
#   (b) Providers must clamp the ``expires_at`` they report to
#       ``requested_at + ready_deadline_seconds + expected_session_seconds``.
#       ``InferenceLease.validate_for`` previously placed no upper bound on
#       expiry and now raises ``InferenceLeaseConstraintError`` past that
#       window, so a provider reporting a coarser natural expiry (e.g. a
#       whole billing hour) that passed under v5 fails under v6. Adding
#       ``touch`` alone does NOT make a provider v6-conformant.
#
# Version 2 (SDK 0.8.0): clarifies the meaning of
# :attr:`ToolCallStarted.index`. The dataclass shape is unchanged
# from version 1, but the documented contract for *consumers* of
# the marker shifted: ``index`` is defined as a provider-native
# value (potentially sparse: Anthropic ``content_block_index``,
# Codex ``output_index``, OpenAI delta-tool-call-index) whose
# *stream order* — not literal value — defines the assembled
# ``LLMResponse.tool_calls`` order. Consumers MUST iterate markers
# in stream order; the previous wording invited
# ``tool_calls[marker.index]`` dispatch, which would mis-fire for
# sparse-index providers. Plugins that pinned ``>= 1`` and never
# read ``marker.index`` directly continue to work; plugins that
# wrote consumer code against the old (positional) wording must
# update to read by stream order.
SDK_LLM_CONTRACT_VERSION = 6

__all__ = [
    "INFERENCE_LEASE_PROVIDER_ENTRY_POINT_GROUP",
    "SDK_LLM_CONTRACT_VERSION",
    "BackendType",
    "BatchHandle",
    "BatchMode",
    "BatchRequest",
    "BatchResult",
    "BatchStatus",
    "CacheMarker",
    "Citation",
    "CodeExecOptions",
    "ComputerUseOptions",
    "FileRef",
    "FilesMode",
    "InferenceLease",
    "InferenceLeaseConstraintError",
    "InferenceLeaseError",
    "InferenceLeaseFailure",
    "InferenceLeaseNotFoundError",
    "InferenceLeaseOwnershipError",
    "InferenceLeaseProvider",
    "InferenceLeaseProviderUnavailableError",
    "InferenceLeaseProvisioningError",
    "InferenceLeaseQuote",
    "InferenceLeaseRequest",
    "InferenceLeaseState",
    "InferencePrivacy",
    "InferenceProviderCapability",
    "InferenceRoute",
    "LLMAdapter",
    "LLMResponse",
    "MCPConnector",
    "ModelCategory",
    "ModelInfo",
    "PromptCacheMode",
    "ProviderCapabilities",
    "ProviderInfo",
    "RawResponse",
    "ReasoningControlMode",
    "RequestOptions",
    "ServerToolMode",
    "ServerToolUse",
    "StructuredOutputMode",
    "TokenCount",
    "TokenCountMode",
    "ToolCall",
    "ToolCallStarted",
    "ToolStreamingMode",
    "VisionInputMode",
    "WebSearchOptions",
]
