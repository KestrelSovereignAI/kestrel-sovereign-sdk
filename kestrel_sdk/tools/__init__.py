"""Kestrel SDK — Tool interfaces."""

from .base import (
    COERCION_FAILED,
    AgentTool,
    ToolCategory,
    ToolParameter,
    ToolSchema,
    ToolExecutionError,
    coerce_json_value,
)
from .parts import current_tool_result_parts, tool_result_parts_buffer
from .result import ToolResult, ToolResultStatus
from .waitable import MonitorableWaitable, Outcome, WaitStatus, Waitable

__all__ = [
    "AgentTool",
    "COERCION_FAILED",
    "coerce_json_value",
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
