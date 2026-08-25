"""Versioned JSON-RPC protocol for isolated feature runtimes.

The transport is line-delimited JSON-RPC 2.0 over child-process stdio. This
keeps the runtime portable across macOS, Linux, and Windows. A future
loopback-TCP transport with an authentication token can reuse these message
types, but stdio is the canonical transport for the SDK contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Any, Literal, TypeAlias

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

# Explicit service declaration used by hosts that may retire an idle child.
# Omission is deliberately distinct from ``False``: legacy or ambiguous
# services must fail resident unless an operator explicitly overrides policy.
INBOUND_PRODUCER_CAPABILITY = "inbound_producer"

# Private, host-to-service ingress. This intentionally lives outside
# ``tools/*``: registrations are control-plane callbacks for the trusted host,
# never feature tools that an agent can discover or invoke.
HOST_INGRESS = "host/ingress"
HOST_INGRESS_CALL = HOST_INGRESS
HOST_INGRESS_CAPABILITY = "host_ingress"
HOST_INGRESS_VERSION = 1
MAX_HOST_INGRESS_NAME_BYTES = 64
MAX_HOST_INGRESS_PAYLOAD_BYTES = 64 * 1024

# ``tools/call`` parameter and initialize capability for trusted invocation
# metadata.  This stays on the normal tool-call envelope: it is deliberately
# not a scheduler-specific RPC surface and is never merged into tool arguments.
TOOL_EXECUTION_CONTEXT = "execution_context"
TOOL_EXECUTION_CONTEXT_CAPABILITY = "tool_execution_context"
TOOL_EXECUTION_CONTEXT_VERSION = 1
MAX_TOOL_EXECUTION_CONTEXT_BYTES = 4096

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


class ToolExecutionContextUnsupportedError(ProtocolError):
    """Raised when a service did not advertise tool execution-context support."""


class HostIngressError(ProtocolError):
    """A private host-ingress request could not be completed safely.

    The public message for this error is deliberately generic. Ingress payloads
    often contain host-only state, so neither a handler exception nor malformed
    input is reflected through the RPC boundary.
    """


class HostIngressUnsupportedError(HostIngressError):
    """Raised when a service does not advertise a compatible ingress contract."""


class HostIngressUnknownNameError(HostIngressUnsupportedError):
    """Raised when a requested ingress name was not advertised by the service."""


HostIngressPayload: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | list["HostIngressPayload"]
    | dict[str, "HostIngressPayload"]
)

_HOST_INGRESS_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_MAX_HOST_INGRESS_PAYLOAD_DEPTH = 32


def validate_host_ingress_name(value: Any) -> str:
    """Validate a bounded, conservative host-ingress slug.

    Names are API identifiers rather than user content. Restricting them to
    lowercase ASCII slug components keeps capability comparison unambiguous and
    prevents a registered callback from being accidentally addressable through
    an alternate spelling. Error messages never include the supplied value.
    """

    if not isinstance(value, str) or not value.isascii():
        raise ProtocolError("host ingress name must be a lowercase slug")
    if len(value) > MAX_HOST_INGRESS_NAME_BYTES:
        raise ProtocolError("host ingress name exceeds the size limit")
    if _HOST_INGRESS_NAME.fullmatch(value) is None:
        raise ProtocolError("host ingress name must be a lowercase slug")
    return value


def _validate_host_ingress_json(value: Any, *, depth: int) -> None:
    """Ensure ``value`` is a strict JSON value without serializing its data."""

    if depth > _MAX_HOST_INGRESS_PAYLOAD_DEPTH:
        raise ProtocolError("host ingress payload exceeds the nesting limit")
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise ProtocolError("host ingress payload must be valid JSON")
        return
    if isinstance(value, list):
        for item in value:
            _validate_host_ingress_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolError("host ingress payload must be valid JSON")
            _validate_host_ingress_json(item, depth=depth + 1)
        return
    raise ProtocolError("host ingress payload must be valid JSON")


def validate_host_ingress_payload(value: Any) -> HostIngressPayload:
    """Validate and size-bound a JSON payload at an ingress trust boundary.

    This is deliberately used by both client and service. The client avoids
    putting oversized or non-JSON data on the wire, while the service treats
    raw JSON-RPC callers as untrusted and repeats exactly the same validation.
    """

    _validate_host_ingress_json(value, depth=0)
    try:
        encoded = json.dumps(
            value,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ProtocolError("host ingress payload must be valid JSON") from exc
    if len(encoded) > MAX_HOST_INGRESS_PAYLOAD_BYTES:
        raise ProtocolError("host ingress payload exceeds the size limit")
    return value


_MAX_CONTEXT_IDENTIFIER_BYTES = 512
_MAX_TRIGGER_KIND_BYTES = 64
_SUPPORTED_TRIGGER_KINDS = frozenset({"scheduler", "event", "agent", "manual", "api"})
_TRIGGER_FIELDS = frozenset(
    {"kind", "id", "source_id", "triggered_at", "scheduled_for"}
)
_CONTEXT_FIELDS = frozenset(
    {"version", "invocation_id", "idempotency_key", "attempt", "trigger"}
)


def _require_context_string(value: Any, field_name: str, *, maximum: int) -> str:
    """Validate a bounded identifier without reflecting its value in errors."""

    if not isinstance(value, str) or not value:
        raise ProtocolError(
            f"tool execution context {field_name} must be a non-empty string"
        )
    if len(value.encode("utf-8")) > maximum:
        raise ProtocolError(
            f"tool execution context {field_name} exceeds the size limit"
        )
    return value


def _optional_context_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_context_string(
        value,
        field_name,
        maximum=_MAX_CONTEXT_IDENTIFIER_BYTES,
    )


def _require_aware_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ProtocolError(
            f"tool execution context {field_name} must be an RFC 3339 timestamp"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProtocolError(
            f"tool execution context {field_name} must include a timezone"
        )
    return value


def _parse_timestamp(value: Any, field_name: str) -> datetime | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > _MAX_CONTEXT_IDENTIFIER_BYTES
    ):
        raise ProtocolError(
            f"tool execution context {field_name} must be an RFC 3339 timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError(
            f"tool execution context {field_name} must be an RFC 3339 timestamp"
        ) from exc
    return _require_aware_timestamp(parsed, field_name)


@dataclass(frozen=True)
class ToolExecutionTrigger:
    """Fixed, non-user-controlled provenance for a tool invocation.

    ``id`` is the durable identifier of the direct trigger; ``source_id`` can
    identify its source (for example, a schedule ID for a scheduler occurrence).
    The two timestamps distinguish when a trigger fired from when it was due.
    No open-ended metadata bag is permitted, so host secrets and arbitrary tool
    input cannot become ambient handler state.
    """

    kind: Literal["scheduler", "event", "agent", "manual", "api"]
    id: str | None = None
    source_id: str | None = None
    triggered_at: datetime | None = None
    scheduled_for: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in _SUPPORTED_TRIGGER_KINDS:
            raise ProtocolError("tool execution context trigger kind is not supported")
        if len(self.kind.encode("utf-8")) > _MAX_TRIGGER_KIND_BYTES:
            raise ProtocolError(
                "tool execution context trigger kind exceeds the size limit"
            )
        _optional_context_string(self.id, "trigger.id")
        _optional_context_string(self.source_id, "trigger.source_id")
        if self.triggered_at is not None:
            _require_aware_timestamp(self.triggered_at, "trigger.triggered_at")
        if self.scheduled_for is not None:
            _require_aware_timestamp(self.scheduled_for, "trigger.scheduled_for")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind}
        if self.id is not None:
            data["id"] = self.id
        if self.source_id is not None:
            data["source_id"] = self.source_id
        if self.triggered_at is not None:
            data["triggered_at"] = self.triggered_at.isoformat()
        if self.scheduled_for is not None:
            data["scheduled_for"] = self.scheduled_for.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolExecutionTrigger":
        if not isinstance(data, dict):
            raise ProtocolError("tool execution context trigger must be an object")
        unknown = set(data).difference(_TRIGGER_FIELDS)
        if unknown:
            raise ProtocolError(
                "tool execution context trigger contains reserved or unknown fields"
            )
        kind = data.get("kind")
        if not isinstance(kind, str):
            raise ProtocolError("tool execution context trigger requires kind")
        return cls(
            kind=kind,
            id=_optional_context_string(data.get("id"), "trigger.id"),
            source_id=_optional_context_string(
                data.get("source_id"), "trigger.source_id"
            ),
            triggered_at=_parse_timestamp(
                data.get("triggered_at"), "trigger.triggered_at"
            ),
            scheduled_for=_parse_timestamp(
                data.get("scheduled_for"), "trigger.scheduled_for"
            ),
        )


@dataclass(frozen=True)
class ToolExecutionContext:
    """Versioned host-authenticated execution metadata for one tool call.

    The stable ``idempotency_key`` is intentionally separate from
    ``invocation_id``: retried deliveries may retain the former while changing
    ``attempt``.  Instances are immutable and validated both when created by a
    host and when decoded by a service at the JSON-RPC trust boundary.
    """

    invocation_id: str
    attempt: int
    trigger: ToolExecutionTrigger
    idempotency_key: str | None = None
    version: int = TOOL_EXECUTION_CONTEXT_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.version, bool)
            or self.version != TOOL_EXECUTION_CONTEXT_VERSION
        ):
            raise ProtocolError("unsupported tool execution context version")
        _require_context_string(
            self.invocation_id,
            "invocation_id",
            maximum=_MAX_CONTEXT_IDENTIFIER_BYTES,
        )
        _optional_context_string(self.idempotency_key, "idempotency_key")
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
        ):
            raise ProtocolError(
                "tool execution context attempt must be a positive integer"
            )
        if not isinstance(self.trigger, ToolExecutionTrigger):
            raise ProtocolError(
                "tool execution context trigger must be a ToolExecutionTrigger"
            )
        encoded = json.dumps(self.to_dict(), separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_TOOL_EXECUTION_CONTEXT_BYTES:
            raise ProtocolError("tool execution context exceeds the size limit")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": self.version,
            "invocation_id": self.invocation_id,
            "attempt": self.attempt,
            "trigger": self.trigger.to_dict(),
        }
        if self.idempotency_key is not None:
            data["idempotency_key"] = self.idempotency_key
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolExecutionContext":
        if not isinstance(data, dict):
            raise ProtocolError("tool execution context must be an object")
        encoded = json.dumps(data, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_TOOL_EXECUTION_CONTEXT_BYTES:
            raise ProtocolError("tool execution context exceeds the size limit")
        unknown = set(data).difference(_CONTEXT_FIELDS)
        if unknown:
            raise ProtocolError(
                "tool execution context contains reserved or unknown fields"
            )
        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ProtocolError("tool execution context requires an integer version")
        invocation_id = _require_context_string(
            data.get("invocation_id"),
            "invocation_id",
            maximum=_MAX_CONTEXT_IDENTIFIER_BYTES,
        )
        attempt = data.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            raise ProtocolError("tool execution context requires an integer attempt")
        trigger = ToolExecutionTrigger.from_dict(data.get("trigger"))
        return cls(
            version=version,
            invocation_id=invocation_id,
            idempotency_key=_optional_context_string(
                data.get("idempotency_key"), "idempotency_key"
            ),
            attempt=attempt,
            trigger=trigger,
        )


@dataclass(frozen=True)
class ToolExecutionContextCapabilities:
    """Execution-context versions a service accepts on ``tools/call``."""

    versions: tuple[int, ...] = (TOOL_EXECUTION_CONTEXT_VERSION,)

    def __post_init__(self) -> None:
        if not self.versions or any(
            isinstance(version, bool) or not isinstance(version, int) or version < 1
            for version in self.versions
        ):
            raise ProtocolError(
                "tool execution context capability requires positive versions"
            )
        if len(set(self.versions)) != len(self.versions):
            raise ProtocolError(
                "tool execution context capability versions must be unique"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"versions": list(self.versions)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolExecutionContextCapabilities":
        if not isinstance(data, dict) or set(data) != {"versions"}:
            raise ProtocolError("tool execution context capability requires versions")
        versions = data.get("versions")
        if not isinstance(versions, list):
            raise ProtocolError(
                "tool execution context capability versions must be a list"
            )
        return cls(versions=tuple(versions))

    def supports(self, version: int) -> bool:
        return version in self.versions


@dataclass(frozen=True)
class HostIngressCapabilities:
    """Versioned private host-ingress names accepted by a service.

    The explicit name list lets a host fail closed before writing an unknown
    callback request to a child. It is intentionally capability metadata only;
    unlike :class:`ToolMetadata`, it is never part of ``tools/list``.
    """

    names: tuple[str, ...]
    version: int = HOST_INGRESS_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or self.version != HOST_INGRESS_VERSION:
            raise ProtocolError("unsupported host ingress capability version")
        if not self.names:
            raise ProtocolError("host ingress capability requires names")
        if len(set(self.names)) != len(self.names):
            raise ProtocolError("host ingress capability names must be unique")
        for name in self.names:
            validate_host_ingress_name(name)

    @property
    def ingress_names(self) -> tuple[str, ...]:
        """Alias for callers that prefer the fully-qualified field name."""

        return self.names

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "names": list(self.names)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HostIngressCapabilities:
        if not isinstance(data, dict) or set(data) != {"version", "names"}:
            raise ProtocolError("host ingress capability requires version and names")
        version = data.get("version")
        names = data.get("names")
        if isinstance(version, bool) or not isinstance(version, int):
            raise ProtocolError("host ingress capability version must be an integer")
        if not isinstance(names, list) or not all(
            isinstance(name, str) for name in names
        ):
            raise ProtocolError(
                "host ingress capability names must be a list of strings"
            )
        return cls(version=version, names=tuple(names))

    def supports(self, name: str) -> bool:
        """Whether this capability accepts ``name`` at the current version."""

        return self.version == HOST_INGRESS_VERSION and name in self.names


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
