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
    ProviderInfo,
    ToolCall,
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

    def test_capability_defaults(self):
        """Defaults match the conservative-by-default invariant: a
        plugin that does not opt into vision / tools is treated as
        not supporting them. Streaming defaults True because most
        modern chat models do."""
        m = ModelInfo(id="x", provider="p", display_name="X")
        assert m.supports_vision is False
        assert m.supports_tools is False
        assert m.supports_streaming is True

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
