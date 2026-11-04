"""Kestrel SDK — Tool interfaces."""

from .base import (
    COERCION_FAILED,
    AgentTool,
    CommandParseError,
    GovernedToolBinding,
    ToolCategory,
    ToolParameter,
    ToolSchema,
    ToolExecutionError,
    coerce_json_value,
    coerce_tool_arguments,
)
from .parts import current_tool_result_parts, tool_result_parts_buffer
from .result import ToolResult, ToolResultStatus
from .deferred import (
    DeferredToolResult, DeferredToolResultHost, FinalizedToolResult,
    ToolPublicationContext, ToolPublicationHost,
)
from .waitable import MonitorableWaitable, Outcome, WaitStatus, Waitable

__all__ = [
    "AgentTool",
    "COERCION_FAILED",
    "CommandParseError",
    "GovernedToolBinding",
    "coerce_json_value",
    "coerce_tool_arguments",
    "ToolCategory",
    "ToolParameter",
    "ToolSchema",
    "ToolExecutionError",
    "ToolResult",
    "ToolResultStatus",
    "DeferredToolResult",
    "DeferredToolResultHost",
    "FinalizedToolResult",
    "ToolPublicationContext",
    "ToolPublicationHost",
    "current_tool_result_parts",
    "tool_result_parts_buffer",
    "Outcome",
    "WaitStatus",
    "Waitable",
    "MonitorableWaitable",
]
