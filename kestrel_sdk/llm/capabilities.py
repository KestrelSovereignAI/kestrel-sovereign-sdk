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
    structured_output_mode: StructuredOutputMode = StructuredOutputMode.NONE
    tool_streaming_mode: ToolStreamingMode = ToolStreamingMode.NONE
    vision_input_mode: VisionInputMode = VisionInputMode.NONE
    embedding_model: str | None = None
    embedding_dim: int | None = None
    model_dependent: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "structured_output_mode",
            "tool_streaming_mode",
            "vision_input_mode",
        ):
            value = data[key]
            if isinstance(value, Enum):
                data[key] = value.value
        data["model_dependent"] = list(self.model_dependent)
        data["notes"] = list(self.notes)
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
