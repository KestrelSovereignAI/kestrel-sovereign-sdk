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
from .model_info import ModelCategory, ModelInfo
from .provider import ProviderInfo
from .response import LLMResponse, ToolCall, ToolCallStarted
from .types import BackendType

# Bumped when the abstract surface of LLMAdapter, the wire shape of
# LLMResponse / ModelInfo / ProviderInfo, or the meaning of the
# ``kestrel_sovereign.llm_providers`` entry-point contract changes in
# a way that requires plugin authors to update their code.
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
SDK_LLM_CONTRACT_VERSION = 2

__all__ = [
    "BackendType",
    "LLMAdapter",
    "LLMResponse",
    "ModelCategory",
    "ModelInfo",
    "ProviderInfo",
    "SDK_LLM_CONTRACT_VERSION",
    "ToolCall",
    "ToolCallStarted",
]
