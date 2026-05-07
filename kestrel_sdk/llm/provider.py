"""Provider/route registration record.

The framework keeps one :class:`ProviderInfo` per successfully
initialized (vendor, route) pair. Routing dispatches to a
``ProviderInfo`` to issue an LLM call: the adapter handles the
protocol, the client is the provider-native SDK object, and ``model``
is the default model for this route.

For the vendor / route / model split, see the docstring on
``kestrel_sovereign.llm.provider_registry``. Promoted to the SDK in
0.5.0 so third-party plugins that want to construct a
``ProviderInfo`` themselves (rather than letting the framework's
auto-factory build one) can do so against a stable contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class ProviderInfo:
    """One initialized route for a vendor.

    A ``ProviderInfo`` represents a (vendor, route) pair. ``name`` is
    the composite key ``"<vendor>:<route>"`` used for routing lookups;
    discovery groups by ``vendor``.

    Attributes:
        name: Composite key. Format ``"<vendor>:<route>"``
            (e.g. ``"anthropic:plan"``, ``"openai:api"``).
        vendor: Vendor identifier (e.g. ``"anthropic"``).
        route: Route within the vendor (e.g. ``"plan"``, ``"api"``).
            For external entry-point providers this is ``"api"``.
        client: Provider-native SDK client (an ``openai.OpenAI``,
            ``anthropic.Anthropic``, etc.). Adapter-opaque to the
            framework — only the matching adapter knows what type
            this is.
        adapter: The :class:`~kestrel_sdk.llm.LLMAdapter` instance
            that handles calls for this route.
        model: Default model id for this route. ``"auto"`` defers to
            discovery; a concrete id pins the route to that model.
        is_cloud: API-served. Mutually exclusive with ``is_local``.
        is_local: Runs on the agent's machine. Used by features that
            care about data egress.
        base_url: Custom endpoint, when the route overrides the
            vendor default. ``None`` means "use the SDK default".
        selection_hints: Free-form labels the model selection layer
            consults when picking between routes (e.g. ``"prefer-cheap"``,
            ``"plan-only"``). Adapter-supplied; routing-meaningful.
    """

    name: str
    vendor: str
    route: str
    client: Any
    adapter: Any
    model: str
    is_cloud: bool = True
    is_local: bool = False
    base_url: Optional[str] = None
    selection_hints: List[str] = field(default_factory=list)
