"""Stable LLM-related types shared across feature packages.

These are lightweight enums and protocols that feature packages
(kestrel-feature-*, kestrel-cloud-*, etc.) need to interact with the
framework's LLM service without depending on the full framework.

The framework re-exports BackendType from this module so existing
callers like ``from kestrel_sovereign.llm.service import BackendType``
keep working unchanged.
"""

from enum import Enum


class BackendType(str, Enum):
    """LLM backend types used by features that switch where the LLM runs.

    - ``CLOUD``: API-served (OpenAI, Anthropic, Gemini, etc.)
    - ``LOCAL``: on-device (Ollama, Piper, etc.)
    - ``REMOTE_GPU``: remote GPU instance the agent owns or rents (RunPod,
      Vast.ai, GCP Compute Engine with GPU, etc.)
    """

    CLOUD = "cloud"
    LOCAL = "local"
    REMOTE_GPU = "remote_gpu"
