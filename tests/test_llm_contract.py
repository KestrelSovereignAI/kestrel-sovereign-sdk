"""Tests for the LLM provider contract.

The shape these tests pin is what every third-party adapter plugin
sees when it imports ``kestrel_sdk.llm``. Any change here is a
contract change — it must be paired with a
:data:`SDK_LLM_CONTRACT_VERSION` bump and a migration note in the
PR body so plugin authors know to update.

See ``kestrel-sovereign#1048`` for the epic this contract supports.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone

import pytest

from kestrel_sdk.llm import (
    SDK_LLM_CONTRACT_VERSION,
    BackendType,
    LLMAdapter,
    LLMResponse,
    ModelCategory,
    ModelInfo,
    ProviderCapabilities,
    ProviderInfo,
    ToolCall,
    ToolCallStarted,
    StructuredOutputMode,
    ToolStreamingMode,
    VisionInputMode,
)


# ---------------------------------------------------------------------------
# Contract version
# ---------------------------------------------------------------------------


class TestContractVersion:
    def test_is_int_at_least_one(self):
        """Pinned: version 1 is the first SDK-LLM contract.

        Plugins that pin `>= 1` rely on this.
        """
        assert isinstance(SDK_LLM_CONTRACT_VERSION, int)
        assert SDK_LLM_CONTRACT_VERSION >= 1


# ---------------------------------------------------------------------------
# ToolCall
# ---------------------------------------------------------------------------


class TestToolCall:
    def test_construction(self):
        tc = ToolCall(id="call_123", name="get_weather", arguments={"city": "SF"})
        assert tc.id == "call_123"
        assert tc.name == "get_weather"
        assert tc.arguments == {"city": "SF"}

    def test_arguments_is_dict_not_string(self):
        """Adapters parse provider-native JSON before constructing —
        downstream code never sees a JSON string."""
        tc = ToolCall(id="x", name="f", arguments={"a": 1})
        assert isinstance(tc.arguments, dict)


# ---------------------------------------------------------------------------
# LLMResponse
# ---------------------------------------------------------------------------


class TestLLMResponse:
    def test_default_construction_is_empty(self):
        resp = LLMResponse()
        assert resp.content is None
        assert resp.tool_calls is None
        assert resp.raw is None
        assert resp.input_tokens is None
        assert resp.output_tokens is None
        assert resp.total_tokens is None
        assert resp.cache_creation_input_tokens is None
        assert resp.cache_read_input_tokens is None

    def test_text_only_response(self):
        resp = LLMResponse(content="hello", input_tokens=10, output_tokens=5, total_tokens=15)
        assert resp.content == "hello"
        assert resp.has_tool_calls is False

    def test_tool_call_response(self):
        tc = ToolCall(id="x", name="f", arguments={})
        resp = LLMResponse(tool_calls=[tc])
        assert resp.has_tool_calls is True

    def test_has_tool_calls_false_for_empty_list(self):
        """Empty list != "model wanted a tool call". Adapters that
        filter zero-length lists must not look like real tool-call
        responses to downstream branching."""
        resp = LLMResponse(tool_calls=[])
        assert resp.has_tool_calls is False

    def test_cache_zero_distinct_from_none(self):
        """0 means 'tried, nothing to cache'; None means 'provider has
        no cache telemetry'. The framework's metrics layer reads this
        distinction."""
        zero = LLMResponse(cache_creation_input_tokens=0)
        none = LLMResponse(cache_creation_input_tokens=None)
        assert zero.cache_creation_input_tokens == 0
        assert none.cache_creation_input_tokens is None
        assert zero.cache_creation_input_tokens != none.cache_creation_input_tokens


# ---------------------------------------------------------------------------
# ModelCategory
# ---------------------------------------------------------------------------


class TestModelCategory:
    def test_values_are_canonical_lowercase(self):
        """Pinned wire format: every catalog JSON file in the wild
        already serializes the bare lowercase string."""
        assert ModelCategory.CHAT.value == "chat"
        assert ModelCategory.EMBEDDING.value == "embedding"
        assert ModelCategory.IMAGE.value == "image"
        assert ModelCategory.AUDIO.value == "audio"
        assert ModelCategory.COMPLETION.value == "completion"

    def test_round_trip_from_string(self):
        assert ModelCategory("chat") is ModelCategory.CHAT

    def test_is_str_subclass(self):
        """``class C(str, Enum)`` makes equality with bare strings
        work — load-bearing for old serialized catalogs that compare
        ``category == 'chat'`` directly."""
        assert ModelCategory.CHAT == "chat"


# ---------------------------------------------------------------------------
# ModelInfo
# ---------------------------------------------------------------------------


class TestModelInfo:
    def test_minimal_construction(self):
        m = ModelInfo(id="x-1", provider="kimi", display_name="Kimi K2")
        assert m.id == "x-1"
        assert m.provider == "kimi"
        assert m.display_name == "Kimi K2"
        assert m.category is ModelCategory.CHAT

    def test_capability_defaults_are_all_conservative(self):
        """Defaults match the conservative-by-default invariant: a
        plugin that does not opt into vision / tools / streaming is
        treated as not supporting them. The streaming default is
        load-bearing: a minimal adapter that overrides only
        ``get_response`` (relying on the SDK's NotImplementedError
        default for ``get_streaming_response``) must not have its
        ``ModelInfo`` advertise streaming, or the framework's
        capability gate would dispatch into the unsupported path."""
        m = ModelInfo(id="x", provider="p", display_name="X")
        assert m.supports_vision is False
        assert m.supports_tools is False
        assert m.supports_streaming is False

    def test_from_dict_streaming_default_preserves_old_catalog_meaning(self):
        """Old catalogs predating the supports_streaming field encoded
        'every chat model streams'. Loading them must keep that
        meaning so we don't silently demote a fleet of cataloged
        models to non-streaming on upgrade.

        Round-trip via to_dict (which always writes the key) is
        unaffected — only catalogs missing the key entirely take
        this path."""
        m = ModelInfo.from_dict({"id": "x", "provider": "p", "display_name": "X"})
        assert m.supports_streaming is True

    def test_to_dict_round_trip_preserves_explicit_streaming_false(self):
        """A fresh ``ModelInfo(...)`` (default supports_streaming=False)
        round-trips exactly — to_dict writes the explicit False, and
        from_dict reads it back without falling into the legacy-True
        path."""
        m = ModelInfo(id="x", provider="p", display_name="X")
        m2 = ModelInfo.from_dict(m.to_dict())
        assert m2.supports_streaming is False

    def test_to_dict_round_trip(self):
        ts = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
        m = ModelInfo(
            id="kimi-k2-turbo",
            provider="kimi",
            display_name="Kimi K2 Turbo",
            category=ModelCategory.CHAT,
            is_featured=True,
            last_used=ts,
            use_count=42,
            supports_tools=True,
            context_limit=131072,
        )
        d = m.to_dict()
        m2 = ModelInfo.from_dict(d)
        assert m2.id == m.id
        assert m2.provider == m.provider
        assert m2.display_name == m.display_name
        assert m2.category is m.category
        assert m2.is_featured is True
        assert m2.last_used == ts
        assert m2.use_count == 42
        assert m2.supports_tools is True
        assert m2.context_limit == 131072

    def test_from_dict_tolerates_missing_optionals(self):
        """Old catalog files that predate newer fields must still load."""
        m = ModelInfo.from_dict(
            {"id": "x", "provider": "p", "display_name": "X"}
        )
        assert m.id == "x"
        assert m.category is ModelCategory.CHAT


# ---------------------------------------------------------------------------
# ProviderInfo
# ---------------------------------------------------------------------------


class TestProviderInfo:
    def test_construction(self):
        p = ProviderInfo(
            name="kimi:api",
            vendor="kimi",
            route="api",
            client=object(),
            adapter=object(),
            model="auto",
        )
        assert p.name == "kimi:api"
        assert p.vendor == "kimi"
        assert p.route == "api"
        assert p.is_cloud is True
        assert p.is_local is False
        assert p.base_url is None
        assert p.selection_hints == []
        assert p.capabilities == ProviderCapabilities()

    def test_selection_hints_is_per_instance_list(self):
        """`field(default_factory=list)` — not a shared mutable default."""
        p1 = ProviderInfo(
            name="a:r", vendor="a", route="r", client=None, adapter=None, model="auto"
        )
        p2 = ProviderInfo(
            name="b:r", vendor="b", route="r", client=None, adapter=None, model="auto"
        )
        p1.selection_hints.append("prefer-cheap")
        assert p2.selection_hints == []

    def test_capabilities_is_per_instance_value(self):
        p1 = ProviderInfo(
            name="a:r", vendor="a", route="r", client=None, adapter=None, model="auto"
        )
        p2 = ProviderInfo(
            name="b:r", vendor="b", route="r", client=None, adapter=None, model="auto"
        )
        assert p1.capabilities is not p2.capabilities
        assert p1.capabilities == p2.capabilities == ProviderCapabilities()


class TestProviderCapabilities:
    def test_defaults_are_conservative(self):
        capabilities = ProviderCapabilities()

        assert capabilities.supports_tools is False
        assert capabilities.supports_streaming is False
        assert capabilities.supports_vision is False
        assert capabilities.supports_structured_output is False
        assert capabilities.structured_output_mode == StructuredOutputMode.NONE
        assert capabilities.tool_streaming_mode == ToolStreamingMode.NONE
        assert capabilities.vision_input_mode == VisionInputMode.NONE
        assert capabilities.model_dependent == ()
        assert capabilities.notes == ()

    def test_to_dict_uses_wire_values(self):
        capabilities = ProviderCapabilities(
            supports_tools=True,
            supports_streaming=True,
            supports_vision=True,
            supports_structured_output=True,
            structured_output_mode=StructuredOutputMode.JSON_SCHEMA,
            tool_streaming_mode=ToolStreamingMode.NATIVE_DELTA,
            vision_input_mode=VisionInputMode.OPENAI_IMAGE_URL,
            model_dependent=("vision",),
            notes=("model-dependent",),
        )

        assert capabilities.to_dict() == {
            "supports_tools": True,
            "supports_streaming": True,
            "supports_vision": True,
            "supports_structured_output": True,
            "structured_output_mode": "json_schema",
            "tool_streaming_mode": "native_delta",
            "vision_input_mode": "openai_image_url",
            "model_dependent": ["vision"],
            "notes": ["model-dependent"],
        }

    def test_from_mapping_accepts_plugin_dicts(self):
        capabilities = ProviderCapabilities.from_mapping(
            {
                "supports_tools": True,
                "supports_streaming": True,
                "supports_vision": True,
                "supports_structured_output": True,
                "structured_output_mode": "json_schema",
                "tool_streaming_mode": "native_delta",
                "vision_input_mode": "openai_image_url",
                "model_dependent": ["vision"],
                "notes": ["plugin"],
            }
        )

        assert capabilities.supports_tools is True
        assert capabilities.structured_output_mode == StructuredOutputMode.JSON_SCHEMA
        assert capabilities.tool_streaming_mode == ToolStreamingMode.NATIVE_DELTA
        assert capabilities.vision_input_mode == VisionInputMode.OPENAI_IMAGE_URL
        assert capabilities.model_dependent == ("vision",)
        assert capabilities.notes == ("plugin",)

    def test_to_dict_round_trips_through_from_mapping(self):
        original = ProviderCapabilities(
            supports_tools=True,
            supports_streaming=True,
            supports_structured_output=True,
            structured_output_mode=StructuredOutputMode.TOOL_FORCED,
            tool_streaming_mode=ToolStreamingMode.NATIVE_DELTA,
            model_dependent=("structured_output",),
            notes=("forced tool",),
        )

        assert ProviderCapabilities.from_mapping(original.to_dict()) == original

    def test_from_mapping_unknown_modes_fall_back_conservatively(self):
        capabilities = ProviderCapabilities.from_mapping(
            {
                "structured_output_mode": "typo",
                "tool_streaming_mode": "typo",
                "vision_input_mode": "typo",
            }
        )

        assert capabilities.structured_output_mode == StructuredOutputMode.NONE
        assert capabilities.tool_streaming_mode == ToolStreamingMode.NONE
        assert capabilities.vision_input_mode == VisionInputMode.NONE


# ---------------------------------------------------------------------------
# BackendType (re-exported from kestrel_sdk.llm.types — pinned here so a
# refactor that drops the re-export is caught by the contract tests).
# ---------------------------------------------------------------------------


class TestBackendType:
    def test_reexported_from_llm_namespace(self):
        assert BackendType.CLOUD.value == "cloud"
        assert BackendType.LOCAL.value == "local"
        assert BackendType.REMOTE_GPU.value == "remote_gpu"


# ---------------------------------------------------------------------------
# LLMAdapter contract
# ---------------------------------------------------------------------------


class TestLLMAdapterIsAbstract:
    def test_cannot_instantiate_directly(self):
        """``get_response`` is abstract — bare ``LLMAdapter()`` is a
        programmer error."""
        with pytest.raises(TypeError):
            LLMAdapter()  # type: ignore[abstract]

    def test_get_response_is_the_only_abstract_method(self):
        """Pinned: a minimal conforming adapter implements only
        ``get_response``. Streaming, listing, and prompt contribution
        are optional."""
        abstracts = LLMAdapter.__abstractmethods__
        assert abstracts == frozenset({"get_response"})

    def test_minimal_subclass_works(self):
        """A text-only, non-streaming, no-tools, no-discovery adapter
        is fully conforming."""

        class EchoAdapter(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                last = messages[-1]["content"] if messages else ""
                return LLMResponse(content=str(last))

        adapter = EchoAdapter()
        assert isinstance(adapter, LLMAdapter)


class TestLLMAdapterOptionalSurface:
    def test_streaming_default_raises(self):
        class A(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

        async def _drive():
            async for _ in A().get_streaming_response(None, "m", []):
                pass

        with pytest.raises(NotImplementedError):
            asyncio.run(_drive())

    def test_list_models_default_raises(self):
        class A(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

        with pytest.raises(NotImplementedError):
            asyncio.run(A().list_models(client=None))

    def test_list_models_receives_route_client(self):
        """Discovery must use the framework's already-initialized
        client so authenticated /models endpoints return the catalog
        that actually matches the route's base_url / auth. Without
        this, an OpenAI-compatible plugin pointed at Kimi or DeepSeek
        would have its discovery hit api.openai.com instead of the
        configured endpoint — silently."""

        seen_clients = []

        class CapturingAdapter(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

            async def list_models(self, client):
                seen_clients.append(client)
                return []

        sentinel = object()
        asyncio.run(CapturingAdapter().list_models(sentinel))
        assert seen_clients == [sentinel]

    def test_streaming_signature_is_async_generator(self):
        """The default impl uses an unreachable ``yield`` so static
        type checkers see ``AsyncIterator[str]``. Verify the shape
        survived: the function must be recognized as an async
        generator function."""
        assert inspect.isasyncgenfunction(LLMAdapter.get_streaming_response)


class TestLLMAdapterContributeSystemPrompt:
    def test_default_returns_base_unchanged(self):
        class A(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

        a = A()
        assert a.contribute_system_prompt("any-model", "base prompt") == "base prompt"
        assert a.contribute_system_prompt("any-model", None) is None

    def test_apply_replaces_first_system_message(self):
        class GPT5Adapter(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

            def contribute_system_prompt(self, model_id, base):
                if not base:
                    base = ""
                return f"[gpt-5 overlay]\n{base}"

        a = GPT5Adapter()
        out = a._apply_system_prompt_contribution(
            [{"role": "system", "content": "you are X"}, {"role": "user", "content": "hi"}],
            "gpt-5-mini",
        )
        assert out[0]["content"] == "[gpt-5 overlay]\nyou are X"
        assert out[1] == {"role": "user", "content": "hi"}

    def test_apply_prepends_when_no_system_message(self):
        class A(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

            def contribute_system_prompt(self, model_id, base):
                return "always-on overlay"

        a = A()
        out = a._apply_system_prompt_contribution(
            [{"role": "user", "content": "hi"}], "any-model"
        )
        assert out[0] == {"role": "system", "content": "always-on overlay"}
        assert out[1] == {"role": "user", "content": "hi"}

    def test_apply_does_not_mutate_input(self):
        class A(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

            def contribute_system_prompt(self, model_id, base):
                return "X"

        a = A()
        original = [{"role": "system", "content": "orig"}]
        a._apply_system_prompt_contribution(original, "m")
        assert original == [{"role": "system", "content": "orig"}]

    def test_apply_no_double_inject_when_string_system_present_and_contribution_unchanged(self):
        """Regression: an adapter that leaves existing system prompts
        unchanged (``contribute(m, base)`` returns ``base``) but would
        inject a non-empty overlay for ``base=None`` must NOT get the
        no-prompt overlay double-stacked on top of an existing
        string-content system message.

        Found by codex review on the Wave 1A PR. The pre-fix code
        only set ``augmented = True`` when the contribution changed
        the content, so a "default-only" adapter would slip through
        the loop unmodified and then trigger the fallback prepend,
        producing two system messages.
        """

        class DefaultOnlyAdapter(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

            def contribute_system_prompt(self, model_id, base):
                if base is None:
                    return "default discipline"
                return base

        a = DefaultOnlyAdapter()
        out = a._apply_system_prompt_contribution(
            [{"role": "system", "content": "user-supplied"}],
            "any-model",
        )
        system_msgs = [m for m in out if m.get("role") == "system"]
        assert len(system_msgs) == 1, (
            f"expected exactly one system message, got {len(system_msgs)}: {out}"
        )
        assert system_msgs[0]["content"] == "user-supplied"

    def test_apply_drops_system_message_when_contribution_is_none(self):
        """An adapter that explicitly suppresses the system prompt
        (returns ``None`` from ``contribute_system_prompt``) must
        result in NO system message in the output — never a system
        message with ``content=None``, which would be an invalid
        wire shape for providers that require string content.

        Found by codex review on the Wave 1A PR (second pass).
        """

        class SuppressingAdapter(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

            def contribute_system_prompt(self, model_id, base):
                return None

        a = SuppressingAdapter()
        out = a._apply_system_prompt_contribution(
            [
                {"role": "system", "content": "user-supplied"},
                {"role": "user", "content": "hi"},
            ],
            "any-model",
        )
        system_msgs = [m for m in out if m.get("role") == "system"]
        assert system_msgs == [], f"expected no system messages, got {system_msgs}"
        # The user message is preserved.
        assert {"role": "user", "content": "hi"} in out

    def test_apply_suppression_does_not_trigger_fallback_prepend(self):
        """Suppression and no-prompt-overlay-injection are different
        intents: an adapter that returns ``None`` to suppress for a
        non-None ``base`` must NOT have its (possibly different)
        ``base=None`` overlay prepended afterward.

        Without this guarantee an adapter could only express "suppress"
        if it also returned ``None`` for ``base=None``, which would
        conflate two different intentions.
        """

        class SelectivelySuppressing(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

            def contribute_system_prompt(self, model_id, base):
                if base is None:
                    return "default-when-empty"  # would inject when no prompt
                return None  # suppress when there is one

        a = SelectivelySuppressing()
        out = a._apply_system_prompt_contribution(
            [{"role": "system", "content": "user"}, {"role": "user", "content": "hi"}],
            "any-model",
        )
        system_msgs = [m for m in out if m.get("role") == "system"]
        assert system_msgs == [], (
            f"suppression must not fall through to the no-prompt overlay; "
            f"got {system_msgs}"
        )

    def test_apply_skips_non_string_system_content(self):
        """Multi-part content (list of blocks) is passed through —
        contributions are text-only by contract."""

        class A(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

            def contribute_system_prompt(self, model_id, base):
                return "should not apply"

        a = A()
        msg = {"role": "system", "content": [{"type": "text", "text": "x"}]}
        out = a._apply_system_prompt_contribution([msg], "m")
        # The non-string system content remained untouched, BUT because
        # no augmentation happened the contribution gets prepended as a
        # fresh system message — that's the documented fallback.
        assert out[0]["role"] == "system"
        assert out[0]["content"] == "should not apply"
        assert out[1] == msg


# ---------------------------------------------------------------------------
# Provider metadata methods (SDK 0.6.0)
#
# All five methods default to ``None``. Plugin authors override the ones
# relevant to their backend; the framework consumes them to remove the
# provider-name string-matching that previously leaked into council
# costing, identity export, model selection, key storage UI, etc.
# ---------------------------------------------------------------------------


class TestLLMAdapterProviderMetadata:
    """All metadata methods default to None — concrete adapters opt in."""

    def _make_minimal_adapter(self):
        class A(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

        return A()

    def test_cost_per_1m_tokens_defaults_to_none(self):
        assert self._make_minimal_adapter().cost_per_1m_tokens() is None

    def test_substrate_type_defaults_to_none(self):
        assert self._make_minimal_adapter().substrate_type() is None

    def test_display_name_defaults_to_none(self):
        assert self._make_minimal_adapter().display_name() is None

    def test_key_env_var_defaults_to_none(self):
        assert self._make_minimal_adapter().key_env_var() is None

    def test_deliberation_style_defaults_to_none(self):
        assert self._make_minimal_adapter().deliberation_style() is None

    def test_provider_capabilities_defaults_to_conservative_value(self):
        assert self._make_minimal_adapter().provider_capabilities() == ProviderCapabilities()


class TestLLMAdapterProviderMetadataOverrides:
    """Concrete adapters that override the metadata methods report
    their values through the same surface plugins use."""

    def test_cost_per_1m_tokens_round_trip(self):
        class PaidAdapter(LLMAdapter):
            async def get_response(self, *a, **kw):
                return LLMResponse()

            def cost_per_1m_tokens(self):
                return {"input": 3.0, "output": 15.0}

        cost = PaidAdapter().cost_per_1m_tokens()
        assert cost == {"input": 3.0, "output": 15.0}

    def test_substrate_type_round_trip(self):
        class ClaudeAdapter(LLMAdapter):
            async def get_response(self, *a, **kw):
                return LLMResponse()

            def substrate_type(self):
                return "claude"

        assert ClaudeAdapter().substrate_type() == "claude"

    def test_display_name_round_trip(self):
        class OpenRouterAdapter(LLMAdapter):
            async def get_response(self, *a, **kw):
                return LLMResponse()

            def display_name(self):
                return "OpenRouter"

        assert OpenRouterAdapter().display_name() == "OpenRouter"

    def test_key_env_var_round_trip(self):
        class KimiAdapter(LLMAdapter):
            async def get_response(self, *a, **kw):
                return LLMResponse()

            def key_env_var(self):
                return "KIMI_API_KEY"

        assert KimiAdapter().key_env_var() == "KIMI_API_KEY"

    def test_deliberation_style_round_trip(self):
        class GroqAdapter(LLMAdapter):
            async def get_response(self, *a, **kw):
                return LLMResponse()

            def deliberation_style(self):
                return "parallel"

        assert GroqAdapter().deliberation_style() == "parallel"

    def test_provider_capabilities_round_trip(self):
        class VisionAdapter(LLMAdapter):
            async def get_response(self, *a, **kw):
                return LLMResponse()

            def provider_capabilities(self):
                return ProviderCapabilities(
                    supports_streaming=True,
                    supports_vision=True,
                    vision_input_mode=VisionInputMode.OPENAI_IMAGE_URL,
                )

        capabilities = VisionAdapter().provider_capabilities()
        assert capabilities.supports_streaming is True
        assert capabilities.supports_vision is True
        assert capabilities.vision_input_mode == VisionInputMode.OPENAI_IMAGE_URL


class TestLLMAdapterMetadataAreNonAbstract:
    """Adding the metadata methods MUST NOT break minimal adapters
    that only implement get_response. The five methods are optional —
    instantiating without overriding any of them must succeed."""

    def test_minimal_adapter_remains_instantiable(self):
        class Echo(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse(content="echo")

        # No NotImplementedError, no TypeError. A plugin that only
        # implements get_response is fully conforming.
        adapter = Echo()
        assert adapter.cost_per_1m_tokens() is None
        assert adapter.substrate_type() is None
        assert adapter.display_name() is None
        assert adapter.key_env_var() is None
        assert adapter.deliberation_style() is None
        assert adapter.provider_capabilities() == ProviderCapabilities()

    def test_abstract_methods_unchanged(self):
        """Pinned: only get_response is abstract. Adding metadata
        methods with default implementations must not promote them
        into the abstract set or invalidate any existing plugin."""
        assert LLMAdapter.__abstractmethods__ == frozenset({"get_response"})


class TestLLMAdapterContractVersion:
    """SDK_LLM_CONTRACT_VERSION pin tracking.

    Versions:
      * 1 — initial contract (SDK 0.5.0 — 0.7.0).
      * 2 — clarified ToolCallStarted.index semantics (SDK 0.8.0):
        index is provider-native; consumers iterate by stream order,
        not ``tool_calls[marker.index]``. Plugins that pinned
        ``>= 1`` and never read ``marker.index`` directly keep
        working; plugins that wrote consumer code against the old
        positional reading must update.
      * 3 — added ProviderCapabilities, ProviderInfo.capabilities, and
        LLMAdapter.provider_capabilities() (SDK 0.17.0).
    """

    def test_contract_version_is_3(self):
        from kestrel_sdk.llm import SDK_LLM_CONTRACT_VERSION

        assert SDK_LLM_CONTRACT_VERSION == 3, (
            "SDK 0.17.0 bumps the LLM contract version from 2 to 3 to "
            "publish provider capability metadata on the shared plugin "
            "surface."
        )


# ---------------------------------------------------------------------------
# ToolCallStarted (SDK 0.7.0)
# ---------------------------------------------------------------------------


class TestToolCallStarted:
    def test_minimal_construction_only_index(self):
        """Pinned: index alone is sufficient. id and name default None,
        which is the documented OpenAI-first-delta case."""
        ev = ToolCallStarted(index=0)
        assert ev.index == 0
        assert ev.id is None
        assert ev.name is None

    def test_full_construction(self):
        """Pinned: anthropic-shape emission with both id and name
        populated at content_block_start."""
        ev = ToolCallStarted(index=2, id="toolu_abc123", name="get_weather")
        assert ev.index == 2
        assert ev.id == "toolu_abc123"
        assert ev.name == "get_weather"

    def test_frozen_immutable(self):
        """Pinned: frozen dataclass — consumers can use as set/dict
        keys when correlating multiple concurrent calls. Mutation
        must raise."""
        ev = ToolCallStarted(index=0)
        with pytest.raises(Exception):
            # FrozenInstanceError on Python 3.11+; broad catch keeps the
            # test stable across versions.
            ev.id = "after-the-fact"  # type: ignore[misc]

    def test_hashable_and_equality(self):
        """Pinned: same fields -> equal -> same hash. Different index
        -> different hash. Load-bearing for the audit hook that may
        keep a ``set[ToolCallStarted]`` of the calls it has seen
        announced this turn."""
        a = ToolCallStarted(index=0, id="x", name="f")
        b = ToolCallStarted(index=0, id="x", name="f")
        c = ToolCallStarted(index=1, id="x", name="f")
        assert a == b
        assert hash(a) == hash(b)
        assert a != c
        assert hash(a) != hash(c)
        # Deduplicates in a set — exactly the property the audit hook needs.
        assert {a, b, c} == {a, c}


# ---------------------------------------------------------------------------
# get_streaming_response_with_tools optional method (SDK 0.7.0)
# ---------------------------------------------------------------------------


class TestStreamingWithToolsOptionalSurface:
    """get_streaming_response_with_tools defaults to NotImplementedError.
    Adapters whose backend supports it override; minimal adapters do not.
    Matches the get_streaming_response / list_models pattern from
    SDK 0.5.0."""

    def test_default_raises(self):
        class A(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

        async def _drive():
            async for _ in A().get_streaming_response_with_tools(
                None, "m", []
            ):
                pass

        with pytest.raises(NotImplementedError):
            asyncio.run(_drive())

    def test_signature_is_async_generator(self):
        """The default uses the same unreachable-yield pattern as
        get_streaming_response so static type checkers recognize it
        as ``AsyncIterator[Union[str, ToolCallStarted, LLMResponse]]``."""
        assert inspect.isasyncgenfunction(
            LLMAdapter.get_streaming_response_with_tools
        )

    def test_adding_method_does_not_change_abstract_set(self):
        """Pinned: adding a new optional method MUST NOT promote it
        into the abstract set. A plugin that implements only
        get_response is still fully conforming under SDK 0.7.0."""

        class Echo(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse(content="echo")

        Echo()  # Does not raise.
        assert LLMAdapter.__abstractmethods__ == frozenset({"get_response"})


class TestStreamingWithToolsOverride:
    """An adapter that does override the method must be able to yield
    text, ToolCallStarted, and a final LLMResponse — the documented
    tagged-union shape."""

    def test_override_yields_full_union(self):
        captured: list = []

        class Streamer(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

            async def get_streaming_response_with_tools(
                self, client, model, messages, **kwargs
            ):
                yield "Let me look that up"
                yield ToolCallStarted(index=0, id="call_1", name="get_weather")
                yield LLMResponse(
                    content="Let me look that up",
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="get_weather",
                            arguments={"city": "SF"},
                        )
                    ],
                    input_tokens=20,
                    output_tokens=8,
                    total_tokens=28,
                )

        async def _drive():
            async for item in Streamer().get_streaming_response_with_tools(
                None, "m", []
            ):
                captured.append(item)

        asyncio.run(_drive())

        # Three items in order: text -> ToolCallStarted -> LLMResponse.
        assert len(captured) == 3
        assert captured[0] == "Let me look that up"
        assert isinstance(captured[1], ToolCallStarted)
        assert captured[1].index == 0
        assert captured[1].id == "call_1"
        assert isinstance(captured[2], LLMResponse)
        assert captured[2].has_tool_calls

    def test_override_text_only_path_does_not_require_llm_response(self):
        """Adapters that finish a stream with text-only output MAY
        terminate without yielding a final LLMResponse — the
        contract says LLMResponse is yielded once when the response
        includes one or more tool calls."""

        class Streamer(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

            async def get_streaming_response_with_tools(
                self, client, model, messages, **kwargs
            ):
                yield "hello "
                yield "world"

        items = []

        async def _drive():
            async for item in Streamer().get_streaming_response_with_tools(
                None, "m", []
            ):
                items.append(item)

        asyncio.run(_drive())
        assert items == ["hello ", "world"]
        # No LLMResponse, no ToolCallStarted — pure text streaming
        # through the same channel is a documented and supported path.

    def test_override_can_emit_multiple_concurrent_tool_calls(self):
        """Codex finding #2: multiple concurrent tool calls must be
        ordered by index. The contract requires that ToolCallStarted
        events with distinct index appear in the same order as the
        corresponding entries in the final LLMResponse.tool_calls."""

        class Streamer(LLMAdapter):
            async def get_response(self, client, model, messages, **kwargs):
                return LLMResponse()

            async def get_streaming_response_with_tools(
                self, client, model, messages, **kwargs
            ):
                yield ToolCallStarted(index=0, id="c0", name="f0")
                yield ToolCallStarted(index=1, id="c1", name="f1")
                yield ToolCallStarted(index=2, id="c2", name="f2")
                yield LLMResponse(
                    tool_calls=[
                        ToolCall(id="c0", name="f0", arguments={}),
                        ToolCall(id="c1", name="f1", arguments={}),
                        ToolCall(id="c2", name="f2", arguments={}),
                    ],
                )

        starts = []
        final = None

        async def _drive():
            nonlocal final
            async for item in Streamer().get_streaming_response_with_tools(
                None, "m", []
            ):
                if isinstance(item, ToolCallStarted):
                    starts.append(item)
                elif isinstance(item, LLMResponse):
                    final = item

        asyncio.run(_drive())
        assert [s.index for s in starts] == [0, 1, 2]
        assert final is not None
        # Each ToolCallStarted's index aligns with its position in
        # the final tool_calls list.
        for i, tc in enumerate(final.tool_calls or []):
            assert starts[i].index == i
            assert starts[i].id == tc.id
            assert starts[i].name == tc.name


class TestStreamingWithToolsContractVersion:
    """Adding ``ToolCallStarted`` + the new optional method in 0.7.0
    was feature-additive (no version bump). The clarification of
    ``ToolCallStarted.index`` semantics in 0.8.0 IS a documented
    contract change and bumps the version to 2."""

    def test_contract_version_is_at_least_2(self):
        assert SDK_LLM_CONTRACT_VERSION >= 2
