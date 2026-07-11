"""Application-specific agent extension contract.

Application packages use this lightweight interface to customize an agent's
prompt and request/response handling without importing the Sovereign runtime.
The runtime remains responsible for deciding where each hook is invoked.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class AppExtension:
    """Base class for application-specific agent behavior."""

    def __init__(self, agent: Any):
        self.agent = agent
        # Compatibility with Sovereign's historic implementation.
        self._agent = agent

    def pre_process_input(self, user_input: str) -> Optional[str]:
        """Return an immediate response to bypass normal processing, if any."""
        return None

    def post_process_response(
        self, response: str, metadata: Dict[str, Any]
    ) -> str:
        """Transform a generated response before it is returned."""
        return response

    def get_system_prompt_prefix(self) -> str:
        """Return application context prepended to the system prompt."""
        return ""

    def get_constitution_amendments(self) -> str:
        """Return application amendments appended to the constitution."""
        return ""


__all__ = ["AppExtension"]
