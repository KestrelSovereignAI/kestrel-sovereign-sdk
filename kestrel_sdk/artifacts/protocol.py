"""Shared artifact operations for tools, uploads, workflows and computer use."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from kestrel_sdk.operator.context import OperatorContext
from .models import (
    ArtifactCommit, ArtifactFile, ArtifactLink, ArtifactPage, ArtifactRef, ArtifactRevision,
    PublicationReceipt,
)


@runtime_checkable
class ArtifactService(Protocol):
    """The host authenticates context; implementations authorize every operation.

Stage is owner-scoped and quota-bounded, commit checks durable content and
current authority, and conflicts retain recoverable staged edits. Returns from
commit must not imply a compute resource has been released. Unknown publication
outcomes use publication_status with the original idempotency key. Link/unlink
does not grant sharing or cascade-delete the canonical artifact. Deletion must
retire dependent views and reconcile bytes according to host retention policy.
Owner scope maps context.tenant_id/principal_id/acting_agent_id exactly. A None
acting_agent_id selects the principal namespace, never all agent namespaces.
publication_status(None) means no receipt is currently visible; the original
commit may still finish. Retain staging and reconcile/retry only the identical
commit with its original key. None never authorizes a new key or discarding
unresolved content.
"""

    async def stage(
        self, context: OperatorContext, content: bytes, *, path: str, media_type: str
    ) -> ArtifactFile: ...
    async def commit(
        self, context: OperatorContext, request: ArtifactCommit
    ) -> PublicationReceipt: ...
    async def publication_status(
        self, context: OperatorContext, idempotency_key: str
    ) -> PublicationReceipt | None: ...
    async def get(
        self, context: OperatorContext, ref: ArtifactRef
    ) -> ArtifactRevision: ...
    async def read(
        self, context: OperatorContext, ref: ArtifactRef, path: str
    ) -> bytes: ...
    async def list(
        self, context: OperatorContext, *, cursor: str | None = None, limit: int = 50
    ) -> ArtifactPage: ...
    async def history(
        self, context: OperatorContext, artifact_id: str, *,
        cursor: str | None = None, limit: int = 50,
    ) -> ArtifactPage: ...
    async def link(self, context: OperatorContext, link: ArtifactLink) -> None: ...
    async def unlink(self, context: OperatorContext, link: ArtifactLink) -> None: ...
    async def delete(self, context: OperatorContext, expected_head: ArtifactRef) -> None: ...
