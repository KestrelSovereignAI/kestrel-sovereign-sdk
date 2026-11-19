"""Registered native tool actions and their content-free identity context.

These in-process values grant no authority. Hosts retain the original live
registration and authorize the target independently of these identity labels.
The action's decidable parameters belong in HookInput.tool_input only.
"""

from dataclasses import asdict, dataclass, field
from functools import partial
import hashlib
from inspect import iscoroutinefunction
import json
import re
import unicodedata
from typing import Any, Awaitable, Callable


NATIVE_ACTION_IDENTITY_BYTES = 1024


def _identity(value: str) -> None:
    if (type(value) is not str or not value
            or any(unicodedata.category(char) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} for char in value)
            or len(value.encode('utf-8')) > NATIVE_ACTION_IDENTITY_BYTES):
        raise ValueError('Native action identity requires bounded control-free text')


def _digest(value: str) -> None:
    if (type(value) is not str or len(value) != 64
            or any(char not in '0123456789abcdef' for char in value)):
        raise ValueError('Native action identity requires a SHA-256 digest')


@dataclass(frozen=True, slots=True, repr=False)
class NativeActionContext:
    """Immutable target attribution; neither a credential nor native params.

    Remote task IDs remain in private custody. The host resolves the actual
    target using its own live authority, then checks this context against that
    target and the exact captured registration. ``schema_digest`` identifies
    the original admitted Task contract, not the action's parameter schema.
    A View label never adopts a
    physical View, and attribution never creates permission.
    """

    action: str
    method: str
    task_handle: str
    remote_task_digest: str
    owner: str
    tool_name: str
    registration_id: str
    schema_digest: str
    attribution_kind: str
    attribution_digest: str
    input_revision: int
    view_label: str | None = None
    conversation_id: str | None = None

    def __post_init__(self) -> None:
        for value in (self.action, self.method, self.task_handle, self.owner,
                      self.tool_name, self.registration_id, self.attribution_kind):
            _identity(value)
        for value in (self.remote_task_digest, self.schema_digest, self.attribution_digest):
            _digest(value)
        if re.fullmatch(r'[a-z][a-z0-9_.:-]*', self.attribution_kind) is None:
            raise ValueError('Native action attribution kind must be a bounded token')
        for value in (self.view_label, self.conversation_id):
            if value is not None:
                _identity(value)
        if type(self.input_revision) is not int or self.input_revision < 0:
            raise ValueError('Native action input revision must be a nonnegative integer')

    def to_dict(self) -> dict[str, Any]:
        """Return detached identity labels for hooks, approval and audit."""
        return asdict(self)


@dataclass(frozen=True, slots=True, repr=False)
class NativeAction:
    """A static declaration captured with its original tool registration.

    The host snapshots declarations during the atomic mount, validates params
    against the captured schema before and after hooks, and calls only this
    registered async handler. An invocation caller never supplies the handler.
    Handlers must be async functions or objects with an async __call__;
    synchronous wrappers returning awaitables must declare an async boundary.
    JSON bytes seal the schema; reading it returns a fresh detached object.
    """

    name: str
    method: str
    schema_json: bytes
    handler: Callable[[NativeActionContext, dict[str, Any]], Awaitable[Any]] = field(repr=False)

    def __post_init__(self) -> None:
        _identity(self.name)
        _identity(self.method)
        if type(self.schema_json) is not bytes:
            raise TypeError('Native action schema must be immutable JSON bytes')
        schema = json.loads(self.schema_json.decode('utf-8'))
        if type(schema) is not dict:
            raise ValueError('Native action schema must be a JSON object')
        sealed = json.dumps(schema, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        object.__setattr__(self, 'schema_json', sealed)
        target = self.handler
        while isinstance(target, partial):
            target = target.func
        if (not callable(self.handler) or isinstance(target, type)
                or not (iscoroutinefunction(target)
                        or iscoroutinefunction(getattr(type(target), '__call__', None)))):
            raise TypeError('Native action requires its registered async handler')

    @property
    def schema(self) -> dict[str, Any]:
        return json.loads(self.schema_json)

    @property
    def schema_digest(self) -> str:
        return hashlib.sha256(self.schema_json).hexdigest()
