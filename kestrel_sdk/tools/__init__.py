"""Kestrel SDK — Tool interfaces."""

from .base import AgentTool, ToolCategory, ToolParameter, ToolSchema, ToolExecutionError
from .result import ToolResult, ToolResultStatus
from .waitable import Outcome, WaitStatus, Waitable

__all__ = [
    "AgentTool",
    "ToolCategory",
    "ToolParameter",
    "ToolSchema",
    "ToolExecutionError",
    "ToolResult",
    "ToolResultStatus",
    "Outcome",
    "WaitStatus",
    "Waitable",
]
