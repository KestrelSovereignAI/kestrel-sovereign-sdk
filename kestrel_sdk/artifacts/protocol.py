"""Shared artifact operations for tools, uploads, workflows and computer use."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from kestrel_sdk.operator.context import OperatorContext
from kestrel_sdk.resources import Identifier
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
head returns the authoritative current revision in the exact authenticated
owner namespace, or ResourceNotFound for an absent/deleted artifact. It must
read the committed head pointer, never infer it from history timestamps or
revision IDs. The returned revision is a snapshot, not a reservation: commit
still compares expected_revision_id because another writer may advance it.
Stage validates publication_key as Identifier, matching the future commit's
idempotency_key, and binds each file to that publication.
An identical key/path/content retry is idempotent; a changed file at that key/path
conflicts while the staging binding is live. Hosts may expire uncommitted
staging; callers must retain original bytes and restage the same file with its
original key before retrying. Commit can consume only that publication's
staged files. Completing
or retiring one publication must not consume another publication's staging.
Stage on a committed or erased key raises ResourceConflict, including identical
restages. Reconcile through publication_status or retry the identical commit;
this stage conflict alone must not be reported as failed publication. For an
erased key, status and commit raise ResourceNotFound; no receipt or bytes are
resurrected and the key is terminal.
A confirmed rolled-back compare-and-swap rejection does not bind the commit key
or discard its bounded staging. After all attempts with that key are resolved,
a caller may rebase expected_revision_id under the same key and staged files.
If it chooses a new key, it must restage every file under that key. Callers must
retain source bytes until publication or explicit discard; a frozen export
counts only while its session and lease are live. Otherwise the broker must
retain an authorized bounded copy. Host staging can expire. An unknown outcome never permits changing the request.
"""

    async def stage(
        self, context: OperatorContext, content: bytes, *, publication_key: Identifier,
        path: str, media_type: str
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
    async def head(
        self, context: OperatorContext, artifact_id: Identifier
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
