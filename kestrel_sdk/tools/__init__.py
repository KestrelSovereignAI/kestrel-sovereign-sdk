"""Kestrel SDK — Tool interfaces."""

from .base import AgentTool, ToolCategory, ToolParameter, ToolSchema, ToolExecutionError
from .parts import current_tool_result_parts, tool_result_parts_buffer
from .result import ToolResult, ToolResultStatus
from .waitable import MonitorableWaitable, Outcome, WaitStatus, Waitable

__all__ = [
    "AgentTool",
    "ToolCategory",
    "ToolParameter",
    "ToolSchema",
    "ToolExecutionError",
    "ToolResult",
    "ToolResultStatus",
    "current_tool_result_parts",
    "tool_result_parts_buffer",
    "Outcome",
    "WaitStatus",
    "Waitable",
    "MonitorableWaitable",
]
