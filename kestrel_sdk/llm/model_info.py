"""Standardized model metadata.

A :class:`ModelInfo` is what an adapter's ``list_models()`` returns.
It is also the row shape the framework persists for
frecency / catalog / "show this in the dropdown" decisions. Third-party
provider plugins build these to describe what their backend offers.

Promoted from ``kestrel_sovereign.llm.model_metadata`` in SDK 0.5.0
unchanged — same field names, same wire shape, same ``to_dict`` /
``from_dict`` pair — so existing serialized catalogs round-trip without
migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class ModelCategory(str, Enum):
    """Top-level kind of a model — drives which UI surface lists it.

    The chat-model dropdown filters to ``CHAT``; embedding-only models
    (``EMBEDDING``) live in feature configs that explicitly want them
    and never appear in chat selection. Image / audio models are
    surfaced by the relevant feature dialogs.

    Wire format is the lowercase ``.value``: existing JSON catalogs
    serialize the bare string (``"chat"``, ``"embedding"``) and rely on
    ``ModelCategory(value)`` for round-trip. Kept as
    ``class C(str, Enum)`` rather than ``StrEnum`` to preserve the
    pre-existing ``str(ModelCategory.CHAT)`` rendering for any caller
    that already depends on it.
    """

    CHAT = "chat"
    EMBEDDING = "embedding"
    IMAGE = "image"
    AUDIO = "audio"
    COMPLETION = "completion"  # legacy base-completion models (pre-chat API)


@dataclass
class ModelInfo:
    """Standardized model information across all providers.

    Required fields come from API discovery (``adapter.list_models()``).
    Optional fields are enriched from config or accumulated from
    runtime usage tracking — the framework merges these in. A plugin
    author should populate the required fields plus any capability
    flags the model genuinely supports; everything else can stay at
    its default.

    Attributes:
        id: API model ID the adapter passes back to the provider on
            invocation (e.g. ``gpt-5-mini``,
            ``claude-sonnet-4-5-20250929``).
        provider: Provider/vendor name as registered in the framework
            (e.g. ``openai``, ``anthropic``, ``ollama``). For external
            plugins this matches the entry-point's advertised
            ``provider_name``.
        display_name: Human-readable name for UI dropdowns. From the
            provider's API when available, with optional config
            overrides.
        category: :class:`ModelCategory` — chat / embedding / etc.

        is_featured: Sorted above non-featured in dropdowns and
            prefixed with ``★``. Set by the catalog service.
        is_hidden: Never shown in the dropdown, even in "Show more".
            Used for retired aliases and internal-only models.
        is_deprecated: The vendor has flagged this model for sunset.
            Surfaces a warning in UIs that still list it.
        is_canonical_alias: ID is a moving pointer (no date suffix) to
            the vendor's current default in a lineage. Treated
            specially by mandate routing — pinning to a canonical
            alias means "always the latest", not a fixed snapshot.

        frecency_score: MRU with decay. Populated from usage tracking;
            adapters do not set this.
        last_used: When this model was last invoked by the agent.
        use_count: Lifetime invocation count. Cheap diagnostic.

        description: Vendor-supplied description, when the API exposes
            one. Free-form text.
        created_at: Vendor-reported creation date as a string. Format
            is provider-specific; the framework does not parse it.

        supports_vision: The model accepts image inputs. The framework
            uses this to gate vision-only feature paths.
        supports_tools: The model accepts function/tool definitions
            and can emit :class:`ToolCall` requests. Filters models
            for tool-using agent loops.
        supports_streaming: ``get_streaming_response`` works for this
            model. Defaults ``False`` (conservative-by-default,
            matching ``supports_vision`` and ``supports_tools``):
            adapters that implement streaming opt in by setting this
            to ``True``. Without that, the framework gates streaming
            off — otherwise a minimal adapter that overrides only
            ``get_response`` would have its ``ModelInfo`` advertise
            streaming, and the framework would dispatch into the
            default ``get_streaming_response`` that raises
            ``NotImplementedError``.

        size_gb: For local models (Ollama, llama.cpp), on-disk size.
            Surfaced in UI. ``None`` for cloud models.
        context_limit: Token context window. Critical for budget
            allocation: the framework's prompt-truncation paths read
            this to decide what fits.
    """

    id: str
    provider: str
    display_name: str
    category: ModelCategory = ModelCategory.CHAT

    is_featured: bool = False
    is_hidden: bool = False
    is_deprecated: bool = False
    is_canonical_alias: bool = False

    frecency_score: float = 0.0
    last_used: Optional[datetime] = None
    use_count: int = 0

    description: Optional[str] = None
    created_at: Optional[str] = None

    supports_vision: bool = False
    supports_tools: bool = False
    supports_streaming: bool = False

    size_gb: Optional[float] = None
    context_limit: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "provider": self.provider,
            "display_name": self.display_name,
            "category": self.category.value,
            "is_featured": self.is_featured,
            "is_hidden": self.is_hidden,
            "is_deprecated": self.is_deprecated,
            "is_canonical_alias": self.is_canonical_alias,
            "frecency_score": self.frecency_score,
            "last_used": self.last_used.isoformat() if self.last_used else None,
            "use_count": self.use_count,
            "description": self.description,
            "created_at": self.created_at,
            "supports_vision": self.supports_vision,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
            "size_gb": self.size_gb,
            "context_limit": self.context_limit,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelInfo":
        """Inverse of :meth:`to_dict`. Tolerates missing optional fields."""
        return cls(
            id=data["id"],
            provider=data["provider"],
            display_name=data.get("display_name", data["id"]),
            category=ModelCategory(data.get("category", "chat")),
            is_featured=data.get("is_featured", False),
            is_hidden=data.get("is_hidden", False),
            is_deprecated=data.get("is_deprecated", False),
            is_canonical_alias=data.get("is_canonical_alias", False),
            frecency_score=data.get("frecency_score", 0.0),
            last_used=(
                datetime.fromisoformat(data["last_used"])
                if data.get("last_used")
                else None
            ),
            use_count=data.get("use_count", 0),
            description=data.get("description"),
            created_at=data.get("created_at"),
            supports_vision=data.get("supports_vision", False),
            supports_tools=data.get("supports_tools", False),
            # Old catalogs predating the supports_streaming field
            # encoded the "every chat model streams" assumption; keep
            # their meaning when the key is absent. Catalogs that DO
            # include the key (everything written after this field
            # was introduced) round-trip exactly via the explicit
            # value. Fresh ModelInfo construction without the kwarg
            # uses the conservative dataclass default (False) instead.
            supports_streaming=data.get("supports_streaming", True),
            size_gb=data.get("size_gb"),
            context_limit=data.get("context_limit"),
        )
