"""Versioned JSON-RPC protocol for isolated feature runtimes.

The transport is line-delimited JSON-RPC 2.0 over child-process stdio. This
keeps the runtime portable across macOS, Linux, and Windows. A future
loopback-TCP transport with an authentication token can reuse these message
types, but stdio is the canonical transport for the SDK contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
import json

JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = "2026-06-17"

INITIALIZE = "initialize"
HEALTH = "health"
TOOLS_LIST = "tools/list"
TOOLS_CALL = "tools/call"
SHUTDOWN = "shutdown"
FEATURE_EVENT = "feature/event"
# Host-only lifecycle operation. This is deliberately outside ``tools/*`` so
# an isolated feature never exposes configuration or lifecycle control to an
# agent-callable tool surface.
CONFIG_TRANSITION = "lifecycle/config-transition"
CONFIG_TRANSITION_CAPABILITY = "config_transition"

CONFIG_TRANSITION_RESTART = "restart"
CONFIG_TRANSITION_APPLIED = "applied"

JsonRpcId = str | int | None


class ProtocolError(ValueError):
    """Raised when a JSON-RPC frame is malformed or unsupported."""


class ConfigTransitionError(ProtocolError):
    """A service rejected or failed a host config-transition request.

    The public error intentionally carries no service exception text or config
    payload. Configuration commonly contains credentials, so neither belongs
    in a host status envelope.
    """


class ConfigTransitionUnsupportedError(ConfigTransitionError):
    """Raised when a legacy service did not advertise config-transition support."""


@dataclass(frozen=True)
class ConfigTransitionCapabilities:
    """Capability metadata negotiated during ``initialize``.

    Every advertising service supports preparing a transition before a
    replacement. ``supports_live_apply`` additionally permits it to return an
    ``applied`` result and remain running with the next configuration.
    """

    supports_live_apply: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "prepare": True,
            "supports_live_apply": self.supports_live_apply,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConfigTransitionCapabilities":
        if data.get("prepare") is not True:
            raise ProtocolError("config_transition capability requires prepare=true")
        supports_live_apply = data.get("supports_live_apply", False)
        if not isinstance(supports_live_apply, bool):
            raise ProtocolError(
                "config_transition capability supports_live_apply must be a boolean"
            )
        return cls(supports_live_apply=supports_live_apply)


@dataclass(frozen=True)
class ConfigTransitionResult:
    """The next action the host must take after a successful transition hook.

    ``restart`` means the service finished its ordered pre-restart cleanup and
    the host must stop and launch a replacement with the next config.
    ``applied`` means the service has live-applied the next config and the host
    may keep the process running. A service may only return ``applied`` after
    advertising ``supports_live_apply``.
    """

    action: Literal["restart", "applied"]

    def __post_init__(self) -> None:
        if self.action not in {CONFIG_TRANSITION_RESTART, CONFIG_TRANSITION_APPLIED}:
            raise ProtocolError("config transition result requires a supported action")

    @classmethod
    def restart_required(cls) -> "ConfigTransitionResult":
        """Return the normal prepare-then-restart outcome."""

        return cls(action=CONFIG_TRANSITION_RESTART)

    @classmethod
    def applied(cls) -> "ConfigTransitionResult":
        """Return the live-apply outcome for an opted-in service."""

        return cls(action=CONFIG_TRANSITION_APPLIED)

    def to_dict(self) -> dict[str, str]:
        return {"action": self.action}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConfigTransitionResult":
        action = data.get("action")
        if action not in {CONFIG_TRANSITION_RESTART, CONFIG_TRANSITION_APPLIED}:
            raise ProtocolError("config transition result requires a supported action")
        return cls(action=action)


@dataclass(frozen=True)
class ToolMetadata:
    """Tool metadata advertised by an isolated feature service."""

    name: str
    description: str
    input_schema: dict[str, Any]
    skill_id: str | None = None
    version: str | None = None
    capability_tags: tuple[str, ...] = field(default_factory=tuple)
    permission_tags: tuple[str, ...] = field(default_factory=tuple)
    category: str | None = None
    command_prefix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
        if self.skill_id is not None:
            data["skill_id"] = self.skill_id
        if self.version is not None:
            data["version"] = self.version
        if self.capability_tags:
            data["capability_tags"] = list(self.capability_tags)
        if self.permission_tags:
            data["permission_tags"] = list(self.permission_tags)
        if self.category is not None:
            data["category"] = self.category
        if self.command_prefix is not None:
            data["command_prefix"] = self.command_prefix
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolMetadata":
        input_schema = data.get("input_schema", data.get("inputSchema"))
        if not isinstance(input_schema, dict):
            raise ProtocolError("tool metadata requires input_schema")
        return cls(
            name=_require_str(data, "name"),
            description=_require_str(data, "description"),
            input_schema=input_schema,
            skill_id=_optional_str(data, "skill_id"),
            version=_optional_str(data, "version"),
            capability_tags=tuple(_optional_str_list(data, "capability_tags")),
            permission_tags=tuple(_optional_str_list(data, "permission_tags")),
            category=_optional_str(data, "category"),
            command_prefix=_optional_str(data, "command_prefix"),
        )


@dataclass(frozen=True)
class JsonRpcRequest:
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: JsonRpcId = None
    jsonrpc: Literal["2.0"] = JSONRPC_VERSION

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "jsonrpc": self.jsonrpc,
            "id": self.id,
            "method": self.method,
        }
        if self.params:
            data["params"] = self.params
        return data


@dataclass(frozen=True)
class JsonRpcNotification:
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    jsonrpc: Literal["2.0"] = JSONRPC_VERSION

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"jsonrpc": self.jsonrpc, "method": self.method}
        if self.params:
            data["params"] = self.params
        return data


@dataclass(frozen=True)
class JsonRpcError:
    code: int
    message: str
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            data["data"] = self.data
        return data


@dataclass(frozen=True)
class JsonRpcResponse:
    id: JsonRpcId
    result: Any = None
    error: JsonRpcError | None = None
    jsonrpc: Literal["2.0"] = JSONRPC_VERSION

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error is not None:
            data["error"] = self.error.to_dict()
        else:
            data["result"] = self.result
        return data


JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse


def encode_message(message: JsonRpcMessage | dict[str, Any]) -> bytes:
    """Encode one JSON-RPC message as a newline-terminated frame."""

    data = message.to_dict() if hasattr(message, "to_dict") else message
    return (json.dumps(data, separators=(",", ":")) + "\n").encode("utf-8")


def decode_message(line: bytes | str) -> JsonRpcMessage:
    """Decode one newline-delimited JSON-RPC frame."""

    if isinstance(line, bytes):
        line = line.decode("utf-8")
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON-RPC frame: {exc.msg}") from exc

    if not isinstance(raw, dict):
        raise ProtocolError("JSON-RPC frame must be an object")
    if raw.get("jsonrpc") != JSONRPC_VERSION:
        raise ProtocolError("JSON-RPC frame must declare jsonrpc='2.0'")

    if "method" in raw:
        method = _require_str(raw, "method")
        params = raw.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ProtocolError("JSON-RPC params must be an object")
        if "id" in raw:
            return JsonRpcRequest(method=method, params=params, id=_parse_id(raw["id"]))
        return JsonRpcNotification(method=method, params=params)

    if "id" not in raw:
        raise ProtocolError("JSON-RPC response requires id")
    if "error" in raw:
        error = raw["error"]
        if not isinstance(error, dict):
            raise ProtocolError("JSON-RPC error must be an object")
        return JsonRpcResponse(
            id=_parse_id(raw["id"]),
            error=JsonRpcError(
                code=_require_int(error, "code"),
                message=_require_str(error, "message"),
                data=error.get("data"),
            ),
        )
    if "result" not in raw:
        raise ProtocolError("JSON-RPC response requires result or error")
    return JsonRpcResponse(id=_parse_id(raw["id"]), result=raw["result"])


def _parse_id(value: Any) -> JsonRpcId:
    if value is None or isinstance(value, (str, int)):
        return value
    raise ProtocolError("JSON-RPC id must be a string, integer, or null")


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{key} must be a non-empty string")
    return value


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{key} must be a non-empty string when provided")
    return value


def _require_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise ProtocolError(f"{key} must be an integer")
    return value


def _optional_str_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProtocolError(f"{key} must be a list of strings when provided")
    return value
