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
from typing import Any, AsyncIterator, Dict, List, Optional, Type

from pydantic import BaseModel

from .model_info import ModelInfo
from .response import LLMResponse


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

    async def list_models(self) -> List[ModelInfo]:
        """Enumerate models this provider exposes.

        Default implementation raises :class:`NotImplementedError`.
        Override in subclasses to call the provider's ``models`` API
        (or, for local backends, to read what's installed). Plugins
        that cannot enumerate dynamically should still implement this
        and return a hand-rolled list — the framework's discovery
        layer treats "raises" and "returns []" differently.

        Returns:
            List of :class:`ModelInfo` objects with model metadata.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support model listing"
        )

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
        """
        new_messages: List[Dict[str, Any]] = []
        augmented = False
        for msg in messages:
            if not augmented and msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str):
                    contributed = self.contribute_system_prompt(model_id, content)
                    if contributed != content:
                        new_messages.append({**msg, "content": contributed})
                        augmented = True
                        continue
            new_messages.append(msg)

        if not augmented:
            contributed = self.contribute_system_prompt(model_id, None)
            if contributed:
                return [{"role": "system", "content": contributed}, *new_messages]
        return new_messages
