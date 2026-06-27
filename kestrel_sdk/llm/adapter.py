"""LLM adapter abstract base — the contract every provider implements.

A :class:`LLMAdapter` standardizes the interface for sending prompts
and receiving responses across LLM providers. Built-in adapters
(OpenAI, Anthropic, Ollama, Vertex, etc.) inherit from this class;
third-party plugins distributed as separate ``pip``-installable
packages do the same and register under the
``kestrel_sovereign.llm_providers`` entry-point group.

The contract is intentionally minimal:

- ``get_response`` is the only abstract method. A text-only,
  non-streaming, no-tools adapter is fully conforming.
- ``get_streaming_response`` and ``list_models`` are optional —
  the default raises :class:`NotImplementedError` and the framework
  is expected to gate streaming / discovery behind a capability
  check before calling them.
- ``contribute_system_prompt`` is a no-op hook adapters can override
  to inject model-family discipline (format hints, behavior contracts)
  into the system prompt without polluting the universal prompt
  builder. Contributions must be byte-stable across turns for any
  given ``model_id`` to preserve prefix-cache invariants
  (kestrel-sovereign issues #703 / #706).

Image handling, message-shape construction, and prompt-cache
breadcrumbing are implementation concerns that live in
``kestrel-sovereign`` (the framework), not the SDK. The SDK ships the
contract; the framework ships the convenience helpers. A plugin that
wants OpenAI-format message construction can either roll its own
(usually a few lines) or import the framework helpers when running
in-tree.

Promoted from ``kestrel_sovereign.llm.adapter`` in SDK 0.5.0. See
:data:`kestrel_sdk.llm.SDK_LLM_CONTRACT_VERSION` for the contract
version a plugin can pin against.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, List, Optional, Type, Union

from pydantic import BaseModel

from .capabilities import ProviderCapabilities
from .model_info import ModelInfo
from .response import (
    BatchHandle,
    BatchRequest,
    BatchResult,
    FileRef,
    LLMResponse,
    RawResponse,
    RequestOptions,
    TokenCount,
    ToolCallStarted,
)


class LLMAdapter(ABC):
    """Abstract base class for LLM provider adapters.

    Subclasses implement :meth:`get_response` at minimum. Adapters that
    support streaming, model discovery, or system-prompt contribution
    override the matching optional hooks.
    """

    @abstractmethod
    async def get_response(
        self,
        client: Any,
        model: str,
        messages: List[Dict[str, Any]],
        format: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[Type[BaseModel]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Issue a chat completion against the provider.

        Args:
            client: Provider-native client (e.g. ``openai.OpenAI``,
                ``anthropic.Anthropic``). Constructed by the framework
                during route initialization and passed back here on
                every call.
            model: Model id to use for this call. The framework
                resolves ``"auto"`` upstream — adapters always receive
                a concrete id.
            messages: Chat messages in OpenAI format. Adapters that
                speak a non-OpenAI shape (Anthropic, Gemini) translate
                internally.
            format: Legacy hint (e.g. ``"json"``). Deprecated; use
                ``response_format`` for structured output.
            tools: Optional list of tools in OpenAI function-calling
                format. The framework normalizes upstream so adapters
                receive a single canonical shape.
            response_format: Optional Pydantic model for structured
                output. When provided, the adapter validates the
                response against this schema before returning.
            **kwargs: Provider-specific parameters (``max_tokens``,
                ``temperature``, etc.). Adapters silently ignore
                kwargs they do not recognize.

        Returns:
            :class:`LLMResponse` with content and/or tool calls and
            (when the provider reports them) usage / cache breakdown.
        """

    async def get_streaming_response(
        self,
        client: Any,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[Type[BaseModel]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream a chat completion as text chunks.

        Default implementation raises :class:`NotImplementedError`.
        Override in subclasses that support streaming. The framework
        is expected to gate streaming on
        :attr:`ModelInfo.supports_streaming` before calling this.

        For text-only streaming. Adapters whose backend can also stream
        tool calls should implement
        :meth:`get_streaming_response_with_tools` (yielding the
        tagged-union of text chunks, :class:`ToolCallStarted` markers,
        and a final :class:`LLMResponse`) rather than smuggling tool
        events through the text-chunk channel.

        Args:
            client: Provider-native client.
            model: Model id.
            messages: Chat messages in OpenAI format.
            tools: Optional tools. Streaming with tools is
                provider-specific; some adapters yield text chunks
                until a tool call is detected and then complete the
                stream.
            response_format: Optional Pydantic model for structured
                output. Streaming with structured output is not
                supported by all providers.
            **kwargs: Provider-specific parameters.

        Yields:
            Text chunks as they arrive.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support streaming"
        )
        # The unreachable yield is required for Python to recognize this
        # as an async generator function so static analysers and the
        # ``AsyncIterator[str]`` return type both check out. Without it
        # the function would be a coroutine returning ``None``.
        if False:  # pragma: no cover
            yield ""

    async def get_streaming_response_with_tools(
        self,
        client: Any,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        response_format: Optional[Type[BaseModel]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[Union[str, ToolCallStarted, LLMResponse]]:
        """Stream a chat completion that may include tool calls.

        Yields a tagged union:

        * :class:`str` — text content chunks as they arrive. Yielded
          live; consumers can render them into a chat UI in real time.
        * :class:`ToolCallStarted` — emitted the moment the provider
          stream first signals a tool call (one event per distinct
          tool-call ``index``). See the class docstring for
          per-provider emission rules. Consumers can treat this as
          "the model is about to invoke a tool — stop assuming the
          preceding text is a final answer." This is the load-bearing
          signal for the constitutional honesty layer
          (kestrel-sovereign #1042 layer 2 / #1045): pre-tool prose
          may be a hallucinated success claim, and the chat client
          should clear or revise the bubble once a
          :class:`ToolCallStarted` arrives.
        * :class:`LLMResponse` — yielded exactly once, at end-of-stream,
          when the response includes one or more tool calls. Source of
          truth for the assembled tool calls (id, name, arguments) and
          for token usage. Adapters that finish the stream with
          text-only output need not yield an :class:`LLMResponse`; the
          stream simply terminates.

        Default implementation raises :class:`NotImplementedError`.
        Override in adapters whose backend supports streaming with
        tools. The framework gates this on
        :attr:`ModelInfo.supports_streaming` AND
        :attr:`ModelInfo.supports_tools` before calling.

        **Ordering invariants** (the contract):

        1. ``ToolCallStarted`` events for distinct ``index`` values
           are yielded in the order their corresponding tool calls
           appear in the final :attr:`LLMResponse.tool_calls`.
        2. Text chunks may be interleaved with ``ToolCallStarted``
           events when the provider stream emits text alongside tool
           deltas (Anthropic mixes text content blocks with tool_use
           blocks; OpenAI may send a leading text segment before the
           first tool delta). Consumers must handle both
           text-before-tool and text-during-tool cases.
        3. The terminal :class:`LLMResponse` is yielded after all text
           chunks and ``ToolCallStarted`` events for the response.
           Consumers can rely on its presence to know the stream is
           complete in the tool-call case.

        **Argument handling for malformed JSON**: when the accumulated
        tool-call argument JSON cannot be parsed at end-of-stream
        (truncated stream, model emitted invalid JSON), adapters
        SHOULD yield the partial string under a sentinel key
        (``{"_raw": "<accumulated string>"}``) in the final
        :class:`LLMResponse`'s ``tool_calls[i].arguments`` rather than
        raising — preserving forward progress so the framework can
        report the error to the model rather than crashing the turn.

        Args:
            client: Provider-native client.
            model: Model id.
            messages: Chat messages in OpenAI format.
            tools: Optional tools in OpenAI function-calling format.
            response_format: Optional Pydantic model for structured
                output. Some adapters (notably Anthropic) implement
                this via the same tool-use mechanism — the framework
                treats the structured-output path as an
                implementation detail of the adapter, not part of the
                streaming contract.
            **kwargs: Provider-specific parameters.

        Yields:
            ``Union[str, ToolCallStarted, LLMResponse]`` per the rules
            above.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support streaming with tools"
        )
        if False:  # pragma: no cover
            yield ""

    async def list_models(self, client: Any) -> List[ModelInfo]:
        """Enumerate models this provider exposes.

        Default implementation raises :class:`NotImplementedError`.
        Override in subclasses to call the provider's ``models`` API
        (or, for local backends, to read what's installed). Plugins
        that cannot enumerate dynamically should still implement this
        and return a hand-rolled list — the framework's discovery
        layer treats "raises" and "returns []" differently.

        Args:
            client: The same provider-native client object the
                framework already passes to :meth:`get_response` for
                this route. Discovery uses the configured client (with
                its ``base_url``, auth, custom headers) so an
                authenticated ``/models`` endpoint returns the catalog
                that actually matches the route. Adapters that build
                their own client from env vars at discovery time would
                hit the wrong endpoint for routes with non-default
                ``base_url`` (Azure, OpenRouter-via-OpenAI-compat,
                Kimi, DeepSeek, etc.). For local backends that do not
                need a client (Ollama hits a fixed URL), this argument
                may be ignored.

        Returns:
            List of :class:`ModelInfo` objects with model metadata.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support model listing"
        )

    async def aembed(
        self,
        client: Any,
        text: str,
        *,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[List[float]]:
        """Generate an embedding for ``text`` using this provider route.

        Default returns ``None`` so chat-only providers remain conforming and
        callers can degrade to keyword search. Providers that expose embedding
        APIs should override this and declare ``supports_embeddings`` plus
        ``embedding_model`` / ``embedding_dim`` in
        :meth:`provider_capabilities`.
        """
        return None

    async def aembed_batch(
        self,
        client: Any,
        texts: List[str],
        *,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Optional[List[float]]]:
        """Generate embeddings for multiple texts.

        Providers can override for native batch APIs. The default preserves a
        single common embedding contract for adapters that only implement
        :meth:`aembed`.
        """
        return [
            await self.aembed(client, text, model=model, **kwargs)
            for text in texts
        ]

    async def count_tokens(
        self,
        client: Any,
        model: str,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> Optional[TokenCount]:
        """Count tokens for a prospective provider request.

        Default returns ``None`` so adapters without a token-counting
        endpoint remain conforming. Adapters that implement this should
        declare ``supports_token_counting`` in :meth:`provider_capabilities`.
        """
        return None

    async def batch_submit(
        self,
        client: Any,
        requests: List[BatchRequest],
        **kwargs: Any,
    ) -> BatchHandle:
        """Submit a provider batch request."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support batch submission"
        )

    async def batch_poll(
        self,
        client: Any,
        handle: BatchHandle,
        **kwargs: Any,
    ) -> BatchHandle:
        """Poll a provider batch handle."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support batch polling"
        )

    async def batch_results(
        self,
        client: Any,
        handle: BatchHandle,
        **kwargs: Any,
    ) -> List[BatchResult]:
        """Fetch completed provider batch results."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support batch results"
        )

    async def batch_cancel(
        self,
        client: Any,
        handle: BatchHandle,
        **kwargs: Any,
    ) -> BatchHandle:
        """Cancel a provider batch handle."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support batch cancellation"
        )

    async def file_upload(
        self,
        client: Any,
        file: Any,
        *,
        purpose: Optional[str] = None,
        **kwargs: Any,
    ) -> FileRef:
        """Upload a file to the provider."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support file upload"
        )

    async def file_list(
        self,
        client: Any,
        **kwargs: Any,
    ) -> List[FileRef]:
        """List files known to the provider route."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support file listing"
        )

    async def file_get(
        self,
        client: Any,
        file_id: str,
        **kwargs: Any,
    ) -> Optional[FileRef]:
        """Fetch one provider file reference if available."""
        return None

    async def file_delete(
        self,
        client: Any,
        file_id: str,
        **kwargs: Any,
    ) -> bool:
        """Delete a provider file."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support file deletion"
        )

    def file_reference(self, file_ref: FileRef) -> Dict[str, Any]:
        """Translate a neutral file reference to provider-native kwargs."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support file references"
        )

    def apply_request_options(
        self,
        request_kwargs: Dict[str, Any],
        options: RequestOptions,
        *,
        model: str,
    ) -> Dict[str, Any]:
        """Translate neutral request options into provider-native kwargs.

        The default returns ``request_kwargs`` unchanged. Providers that
        support prompt-cache shaping, reasoning controls, or server-managed
        tools can override this without changing ``get_response``.
        """
        return request_kwargs

    async def raw_request(
        self,
        client: Any,
        operation: str,
        payload: Optional[Any] = None,
        *,
        http_method: Optional[str] = None,
        path: Optional[str] = None,
        **kwargs: Any,
    ) -> RawResponse:
        """Run a provider-native operation through the initialized client."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support raw passthrough"
        )

    def contract_features(self) -> frozenset[str]:
        """Return v5 optional contract features this adapter implements."""
        return frozenset()

    def contribute_system_prompt(
        self,
        model_id: str,
        base: Optional[str],
    ) -> Optional[str]:
        """Augment the system prompt with model-specific discipline.

        Default returns ``base`` unchanged. Subclasses override to inject
        behavior contracts, format hints, or model-family discipline
        that does not belong in the universal system prompt.

        The contribution **must be byte-stable across turns** for any
        given ``model_id``. The framework's prompt cache (kestrel-sovereign
        #703 / #706) is position-indexed, not longest-prefix-matching,
        so any drift here breaks cache hits silently.

        Args:
            model_id: Concrete model id this turn will hit. Lets a
                single adapter contribute different overlays per model
                family (e.g. GPT-5 vs GPT-4o).
            base: The framework's universal system prompt for this
                turn, or ``None`` if the conversation has none. Adapters
                that always inject discipline should handle the
                ``None`` case by returning their contribution alone.

        Returns:
            The (possibly augmented) system prompt, or ``None`` to
            signal "no system prompt for this turn".
        """
        return base

    def _apply_system_prompt_contribution(
        self,
        messages: List[Dict[str, Any]],
        model_id: str,
    ) -> List[Dict[str, Any]]:
        """Apply :meth:`contribute_system_prompt` to a message list.

        Returns a new list — does not mutate the input. The first
        ``system``-role message has its content replaced by
        ``contribute_system_prompt(model_id, original)``. If no system
        message is present and the contribution is non-empty, a new
        system message is prepended.

        This helper is provided on the base class so adapters can call
        it from :meth:`get_response` without re-implementing the
        merge logic. It only handles ``content``-as-``str`` system
        messages — multi-part content is passed through unchanged
        because the contribution shape is text-only.

        The fallback prepend (``contribute_system_prompt(model_id, None)``
        when no system message is present) only fires when there is
        genuinely no string-content system message. An adapter that
        leaves existing prompts alone (``base`` returned unchanged)
        but would inject a different overlay for ``base=None`` does
        NOT get its no-prompt overlay double-stacked on top of the
        existing one — encountering a string-content system message
        marks the slot as filled, regardless of whether the
        contribution rewrote it.

        If the adapter returns ``None`` from
        :meth:`contribute_system_prompt` for an existing system
        message, that is the documented signal "no system prompt for
        this turn" — the helper drops the system message entirely
        rather than emitting an invalid ``{"content": None}`` shape.
        Suppression also marks the slot as filled, so the no-prompt
        fallback does not fire afterwards (suppression and overlay
        injection are different intents).
        """
        new_messages: List[Dict[str, Any]] = []
        augmented = False
        for msg in messages:
            if not augmented and msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str):
                    contributed = self.contribute_system_prompt(model_id, content)
                    if contributed is None:
                        # Documented "suppress this turn" signal —
                        # drop the system message rather than keep
                        # it with content=None.
                        pass
                    elif contributed != content:
                        new_messages.append({**msg, "content": contributed})
                    else:
                        new_messages.append(msg)
                    augmented = True
                    continue
            new_messages.append(msg)

        if not augmented:
            contributed = self.contribute_system_prompt(model_id, None)
            if contributed:
                return [{"role": "system", "content": contributed}, *new_messages]
        return new_messages

    # ------------------------------------------------------------------
    # Provider metadata — optional, all default to ``None`` so existing
    # adapters need not implement them. Each is consumed by a specific
    # framework subsystem (council costing, identity export, model
    # selection, key storage UI, etc.). Implement the ones relevant to
    # your backend; the framework falls back to a sensible default when
    # ``None`` is returned.
    #
    # Promoted in SDK 0.6.0 to kill the provider-name string-matching
    # leaks in ``kestrel-sovereign`` (council pricing tables, substrate
    # mapping, identity export, service-key UI, etc. — see kestrel-
    # sovereign #1048 Wave 2). Pre-0.6.0, those subsystems consulted
    # hardcoded dicts keyed by provider name; with these methods,
    # third-party plugins are first-class participants.
    # ------------------------------------------------------------------

    def cost_per_1m_tokens(self) -> Optional[Dict[str, float]]:
        """Return token pricing for this adapter's primary model family.

        Format: ``{"input": <USD per 1M input tokens>, "output": <USD
        per 1M output tokens>}``, or ``None`` if pricing is unknown,
        not applicable (local backends), or varies by model.

        Used by ``kestrel-sovereign``'s council deliberation cost
        accounting. ``None`` means "treat as unknown" — the framework's
        cost-aware routing falls back to a conservative default rather
        than guessing.

        For adapters whose pricing varies meaningfully by model, prefer
        carrying per-model pricing on the ``ModelInfo`` returned by
        :meth:`list_models` and leaving this method at ``None``.
        """
        return None

    def substrate_type(self) -> Optional[str]:
        """Return a stable identifier for the model family / substrate.

        A short lowercase string the framework uses for substrate-aware
        decisions and identity export — e.g. ``"claude"``, ``"gpt"``,
        ``"gemini"``, ``"llama"``, ``"mistral"``. Plugin authors should
        return whatever short identifier captures the underlying weights
        family their backend serves.

        ``None`` means "substrate is heterogeneous, unknown, or not
        meaningful". The framework treats unknown substrates as
        ``OPENAI_COMPATIBLE`` for downstream defaults (message shape,
        tool format) — that's a reasonable assumption for most
        OpenAI-shape API providers.
        """
        return None

    def display_name(self) -> Optional[str]:
        """Return a human-readable provider name for UI surfaces.

        Surfaces in the model dropdown, the service-keys settings
        panel, and audit logs. ``None`` falls back to the entry-point
        name (e.g. ``"kimi"``) titlecased — fine for most plugins, but
        override when the provider's brand name doesn't match its
        package name (``"OpenRouter"`` vs ``"openrouter"``).
        """
        return None

    def key_env_var(self) -> Optional[str]:
        """Return the conventional env var name for this provider's API key.

        Used by ``kestrel-sovereign``'s service-key storage and
        diagnostics to surface which env var corresponds to which
        adapter. Format: an ``UPPER_SNAKE_CASE`` identifier (e.g.
        ``"OPENAI_API_KEY"``, ``"ANTHROPIC_API_KEY"``,
        ``"KIMI_API_KEY"``).

        ``None`` for adapters that don't authenticate via API key
        (Ollama, local llama.cpp, OAuth-based plans), or for adapters
        whose auth scheme doesn't fit the env-var pattern.
        """
        return None

    def deliberation_style(self) -> Optional[str]:
        """Return a hint for council deliberation routing.

        ``"parallel"`` — fast/cheap models good for breadth-first
        deliberation rounds run in parallel.
        ``"sequential"`` — slower/more expensive models suited to a
        single careful pass.
        ``None`` — no preference; the framework picks a default.

        Plugin authors can use this to nudge the council toward
        running their model in the role best suited to its
        cost/quality profile. The framework treats this as a hint,
        not a hard constraint.
        """
        return None

    def provider_capabilities(self) -> ProviderCapabilities:
        """Return adapter-level feature capabilities.

        This describes the provider route surface, not a specific model.
        Adapters should override this when they support tools, streaming,
        vision, or structured output. The default is conservative so minimal
        text-only plugins remain valid.
        """
        return ProviderCapabilities()
