"""Optional authenticated file transfer extension of a computer driver."""

from typing import Protocol, runtime_checkable

from pydantic import SecretBytes

from kestrel_sdk.artifacts import ArtifactFile
from kestrel_sdk.computer_use.models import ComputerSession
from .models import (
    WorkspaceExportRequest, WorkspaceImportRequest, WorkspaceMaterialization,
    WorkspaceSnapshot, WorkspaceTransferStatus,
)


@runtime_checkable
class WorkspaceTransfer(Protocol):
    """A driver-owned transport, selected only through host capability admission.

The host artifact service owns authorization, staging and publication.
The driver owns filesystem containment, regular-file checks, byte/hash bounds,
immutable snapshots, atomic installation, idempotency and physical cleanup.
No unrestricted shell fallback is permitted. No new network endpoint is implied.

Data operations require the admitted session paused with controller none; check
current identity/epoch/expiry at every boundary. This fences automated/human
input during transfer. A takeover/revocation fences reads/writes immediately.
Retirement operations remain available to the authenticated cleanup owner after
expiry/revocation; cleanup does not authorize reading or creating new content.

Prepare methods durably bind a transfer ID to its canonical request digest.
Changed retries conflict. Unknown outcomes reconcile the same ID with status;
None means no currently visible record, not proof the first call cannot finish.
An identical prepare_export retry returns the original frozen snapshot, never
freezes again; status also carries that snapshot when ready/transferring. After
release, the same prepare ID raises ResourceNotFound instead of exporting again.
prepare_export during releasing raises ResourceConflict. An identical
prepare_import retry after complete returns the original complete status and
receipt without writing/installing again; aborting raises ResourceConflict,
and aborted raises ResourceNotFound without allocating new staging. These are
reconciliation results subject to current metadata-read authority, not new data
operations or an exemption from revocation.
Exports progress preparing -> ready/transferring -> releasing -> released;
imports progress preparing -> ready/transferring -> complete or aborting ->
aborted. Unknown requires reconciliation, never an inferred terminal outcome.
File retries verify exact manifest path/hash/size; completion requires every
file and atomic installation. Partial content never becomes a live workspace.
"""

    async def prepare_export(
        self, session: ComputerSession, request: WorkspaceExportRequest
    ) -> WorkspaceSnapshot: ...
    async def read_export(
        self, session: ComputerSession, snapshot_id: str, path: str
    ) -> SecretBytes: ...
    async def release_export(
        self, session: ComputerSession, transfer_id: str
    ) -> WorkspaceTransferStatus: ...
    async def prepare_import(
        self, session: ComputerSession, request: WorkspaceImportRequest
    ) -> WorkspaceTransferStatus: ...
    async def write_import_file(
        self, session: ComputerSession, transfer_id: str,
        file: ArtifactFile, content: SecretBytes,
    ) -> WorkspaceTransferStatus: ...
    async def commit_import(
        self, session: ComputerSession, transfer_id: str
    ) -> WorkspaceMaterialization: ...
    async def abort_import(
        self, session: ComputerSession, transfer_id: str
    ) -> WorkspaceTransferStatus: ...
    async def status(
        self, session: ComputerSession, transfer_id: str
    ) -> WorkspaceTransferStatus | None: ...


@runtime_checkable
class WorkspaceTransferProvider(Protocol):
    """Optional extension negotiated in addition to the driver's files capability."""

    def workspace_transfer(self) -> WorkspaceTransfer: ...
