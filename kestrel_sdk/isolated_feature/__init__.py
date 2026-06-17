"""Isolated feature stdio JSON-RPC runtime contract."""

from .client import IsolatedFeatureClient, SubprocessIsolatedFeatureClient
from .protocol import (
    FEATURE_EVENT,
    HEALTH,
    INITIALIZE,
    JSONRPC_VERSION,
    PROTOCOL_VERSION,
    SHUTDOWN,
    TOOLS_CALL,
    TOOLS_LIST,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    ProtocolError,
    ToolMetadata,
    decode_message,
    encode_message,
)
from .service import IsolatedFeatureService

__all__ = [
    "FEATURE_EVENT",
    "HEALTH",
    "INITIALIZE",
    "JSONRPC_VERSION",
    "PROTOCOL_VERSION",
    "SHUTDOWN",
    "TOOLS_CALL",
    "TOOLS_LIST",
    "IsolatedFeatureClient",
    "IsolatedFeatureService",
    "JsonRpcError",
    "JsonRpcNotification",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "ProtocolError",
    "SubprocessIsolatedFeatureClient",
    "ToolMetadata",
    "decode_message",
    "encode_message",
]

