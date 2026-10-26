"""Immutable resource facts shared by environments and durable artifacts.

These records never authenticate a caller. Hosts resolve identity from their
authenticated context and authorize every operation before using a provider.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, Strict

from kestrel_sdk._validation import stable_token


def _identifier(value: str) -> str:
    return stable_token(value, "identifier")


Identifier = Annotated[str, AfterValidator(_identifier)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
USD = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class ResourceContract(BaseModel):
    """Strict immutable v1 record; error text suppresses field input details.

    Non-SDK enclosing models/TypeAdapters must configure their own error hiding;
    invalid discriminator tags and unknown field names can still appear. Log
    exception classes only; structured ValidationError details remain private.
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, validate_default=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )
    schema_version: Annotated[int, Strict(), Field(ge=1, le=1)] = 1


class ResourceScope(ResourceContract):
    """Durable owner facts, independent of a conversation, run or machine.

``principal_id`` is the authenticated user's/operator's stable identity;
``agent_id`` is the optional owning agent. None is the principal's own namespace
for uploads made without an agent; it is never a wildcard or permission to read
agent-owned artifacts. Hosts map OperatorContext.acting_agent_id exactly, and
must not invent an agent or infer ownership from provenance/context links.
"""

    tenant_id: Identifier
    principal_id: Identifier
    agent_id: Identifier | None = None


class ResourceError(Exception):
    """Base error for resource operations; messages must not contain secrets."""


class ResourceNotFound(ResourceError):
    """Resource absent from the authorized scope (also hides other owners)."""


class ResourceConflict(ResourceError):
    """Expected revision/control epoch or an idempotency payload did not match."""


class ResourceUnsupported(ResourceError):
    """An explicitly requested capability is not available; do not fall back."""


class ResourceOutcomeUnknown(ResourceError):
    """Work may have been accepted; reconcile before any potentially unsafe retry."""
