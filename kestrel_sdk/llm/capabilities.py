"""Provider-level LLM capability metadata.

``ModelInfo`` describes an individual model returned by discovery. These
capabilities describe the adapter/route contract: whether the provider can ask
for tools, stream, send images, or request schema-constrained output at all.
Per-model metadata remains authoritative for model-dependent providers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping


class StructuredOutputMode(str, Enum):
    """How an adapter implements typed / schema-constrained output."""

    NONE = "none"
    JSON_OBJECT = "json_object"
    JSON_SCHEMA = "json_schema"
    PROVIDER_NATIVE = "provider_native"
    SCHEMA_FORMAT = "schema_format"
    TOOL_FORCED = "tool_forced"
    UNKNOWN = "unknown"


class ToolStreamingMode(str, Enum):
    """How tool calls are surfaced when the caller requests streaming."""

    NONE = "none"
    NATIVE_DELTA = "native_delta"
    NONSTREAM_FALLBACK = "nonstream_fallback"
    INLINE_EXECUTOR = "inline_executor"
    UNKNOWN = "unknown"


class VisionInputMode(str, Enum):
    """Provider wire format used for image input."""

    NONE = "none"
    OPENAI_IMAGE_URL = "openai_image_url"
    ANTHROPIC_CONTENT_BLOCK = "anthropic_content_block"
    GEMINI_INLINE_DATA = "gemini_inline_data"
    OLLAMA_IMAGES = "ollama_images"
    PROVIDER_NATIVE = "provider_native"
    UNKNOWN = "unknown"


class ReasoningControlMode(str, Enum):
    """How an adapter exposes provider reasoning controls."""

    NONE = "none"
    EFFORT = "effort"
    THINKING_BUDGET = "thinking_budget"
    PROVIDER_NATIVE = "provider_native"
    UNKNOWN = "unknown"


class PromptCacheMode(str, Enum):
    """How prompt-cache shaping is requested."""

    NONE = "none"
    AUTOMATIC = "automatic"
    EXPLICIT_BREAKPOINTS = "explicit_breakpoints"
    PROVIDER_NATIVE = "provider_native"
    UNKNOWN = "unknown"


class BatchMode(str, Enum):
    """How batch requests are submitted."""

    NONE = "none"
    PROVIDER_NATIVE = "provider_native"
    FILE_BASED = "file_based"
    UNKNOWN = "unknown"


class FilesMode(str, Enum):
    """How provider file APIs are exposed."""

    NONE = "none"
    PROVIDER_NATIVE = "provider_native"
    UPLOAD = "upload"
    REFERENCE_ONLY = "reference_only"
    UNKNOWN = "unknown"


class TokenCountMode(str, Enum):
    """How token counting is implemented."""

    NONE = "none"
    PROVIDER_NATIVE = "provider_native"
    ESTIMATE = "estimate"
    UNKNOWN = "unknown"


class ServerToolMode(str, Enum):
    """How server-managed tools are invoked."""

    NONE = "none"
    PROVIDER_NATIVE = "provider_native"
    REQUEST_OPTION = "request_option"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderCapabilities:
    """Adapter-level capabilities for one initialized provider route.

    The coarse booleans answer "can this adapter ask for the feature at all?"
    The mode fields say how it is implemented, and ``model_dependent`` records
    capabilities that still vary by selected model or upstream route.
    """

    supports_tools: bool = False
    supports_streaming: bool = False
    supports_vision: bool = False
    supports_structured_output: bool = False
    supports_embeddings: bool = False
    supports_inline_system: bool = False
    structured_output_mode: StructuredOutputMode = StructuredOutputMode.NONE
    tool_streaming_mode: ToolStreamingMode = ToolStreamingMode.NONE
    vision_input_mode: VisionInputMode = VisionInputMode.NONE
    embedding_model: str | None = None
    embedding_dim: int | None = None
    model_dependent: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    supports_token_counting: bool = False
    supports_batch: bool = False
    supports_files: bool = False
    supports_prompt_cache: bool = False
    supports_reasoning_control: bool = False
    supports_web_search: bool = False
    supports_code_execution: bool = False
    supports_computer_use: bool = False
    supports_mcp_connector: bool = False
    supports_citations: bool = False
    supports_fine_grained_tool_streaming: bool = False
    supports_raw_passthrough: bool = False
    reasoning_control_mode: ReasoningControlMode = ReasoningControlMode.NONE
    prompt_cache_mode: PromptCacheMode = PromptCacheMode.NONE
    batch_mode: BatchMode = BatchMode.NONE
    files_mode: FilesMode = FilesMode.NONE
    token_count_mode: TokenCountMode = TokenCountMode.NONE
    server_tool_mode: ServerToolMode = ServerToolMode.NONE
    max_thinking_budget_tokens: int | None = None
    reasoning_effort_levels: tuple[str, ...] = ()
    max_cache_breakpoints: int | None = None
    raw_operations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "structured_output_mode",
            "tool_streaming_mode",
            "vision_input_mode",
            "reasoning_control_mode",
            "prompt_cache_mode",
            "batch_mode",
            "files_mode",
            "token_count_mode",
            "server_tool_mode",
        ):
            value = data[key]
            if isinstance(value, Enum):
                data[key] = value.value
        data["model_dependent"] = list(self.model_dependent)
        data["notes"] = list(self.notes)
        data["reasoning_effort_levels"] = list(self.reasoning_effort_levels)
        data["raw_operations"] = list(self.raw_operations)
        return data

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ProviderCapabilities":
        return cls(
            supports_tools=bool(data.get("supports_tools", False)),
            supports_streaming=bool(data.get("supports_streaming", False)),
            supports_vision=bool(data.get("supports_vision", False)),
            supports_structured_output=bool(
                data.get("supports_structured_output", False)
            ),
            supports_embeddings=bool(data.get("supports_embeddings", False)),
            supports_inline_system=bool(
                data.get("supports_inline_system", False)
            ),
            structured_output_mode=_enum_value(
                StructuredOutputMode,
                data.get("structured_output_mode"),
                StructuredOutputMode.NONE,
            ),
            tool_streaming_mode=_enum_value(
                ToolStreamingMode,
                data.get("tool_streaming_mode"),
                ToolStreamingMode.NONE,
            ),
            vision_input_mode=_enum_value(
                VisionInputMode,
                data.get("vision_input_mode"),
                VisionInputMode.NONE,
            ),
            embedding_model=(
                str(data["embedding_model"])
                if data.get("embedding_model") is not None
                else None
            ),
            embedding_dim=_positive_int_or_none(data.get("embedding_dim")),
            model_dependent=tuple(data.get("model_dependent") or ()),
            notes=tuple(data.get("notes") or ()),
            supports_token_counting=bool(
                data.get("supports_token_counting", False)
            ),
            supports_batch=bool(data.get("supports_batch", False)),
            supports_files=bool(data.get("supports_files", False)),
            supports_prompt_cache=bool(
                data.get("supports_prompt_cache", False)
            ),
            supports_reasoning_control=bool(
                data.get("supports_reasoning_control", False)
            ),
            supports_web_search=bool(
                data.get("supports_web_search", False)
            ),
            supports_code_execution=bool(
                data.get("supports_code_execution", False)
            ),
            supports_computer_use=bool(
                data.get("supports_computer_use", False)
            ),
            supports_mcp_connector=bool(
                data.get("supports_mcp_connector", False)
            ),
            supports_citations=bool(data.get("supports_citations", False)),
            supports_fine_grained_tool_streaming=bool(
                data.get("supports_fine_grained_tool_streaming", False)
            ),
            supports_raw_passthrough=bool(
                data.get("supports_raw_passthrough", False)
            ),
            reasoning_control_mode=_enum_value(
                ReasoningControlMode,
                data.get("reasoning_control_mode"),
                ReasoningControlMode.NONE,
            ),
            prompt_cache_mode=_enum_value(
                PromptCacheMode,
                data.get("prompt_cache_mode"),
                PromptCacheMode.NONE,
            ),
            batch_mode=_enum_value(
                BatchMode,
                data.get("batch_mode"),
                BatchMode.NONE,
            ),
            files_mode=_enum_value(
                FilesMode,
                data.get("files_mode"),
                FilesMode.NONE,
            ),
            token_count_mode=_enum_value(
                TokenCountMode,
                data.get("token_count_mode"),
                TokenCountMode.NONE,
            ),
            server_tool_mode=_enum_value(
                ServerToolMode,
                data.get("server_tool_mode"),
                ServerToolMode.NONE,
            ),
            max_thinking_budget_tokens=_positive_int_or_none(
                data.get("max_thinking_budget_tokens")
            ),
            reasoning_effort_levels=tuple(
                data.get("reasoning_effort_levels") or ()
            ),
            max_cache_breakpoints=_positive_int_or_none(
                data.get("max_cache_breakpoints")
            ),
            raw_operations=tuple(data.get("raw_operations") or ()),
        )


def _enum_value(
    enum_type: type[Enum],
    value: Any,
    default: Enum,
) -> Enum:
    if isinstance(value, enum_type):
        return value
    if value is None:
        return default
    try:
        return enum_type(str(value))
    except ValueError:
        return default


def _positive_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
