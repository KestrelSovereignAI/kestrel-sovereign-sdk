"""Bounded workspace transfer records. None of these records grants access."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, AwareDatetime, Field, model_validator

from kestrel_sdk.artifacts import ArtifactFile, ArtifactManifest, ArtifactRef
from kestrel_sdk.resources import Identifier, NonNegativeInt, PositiveInt, ResourceContract

WorkspacePath = Annotated[str, AfterValidator(ArtifactFile.contained_path)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class TransferLimits(ResourceContract):
    max_files: Annotated[int, Field(ge=1, le=1024)]
    max_file_bytes: PositiveInt
    max_total_bytes: PositiveInt

    @model_validator(mode="after")
    def consistent_bounds(self) -> Self:
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("per-file transfer limit exceeds total limit")
        return self

    def check(self, manifest: ArtifactManifest) -> None:
        if (len(manifest.files) > self.max_files
                or any(file.size_bytes > self.max_file_bytes for file in manifest.files)
                or sum(file.size_bytes for file in manifest.files) > self.max_total_bytes):
            raise ValueError("manifest exceeds authorized transfer limits")


class WorkspaceRequest(ResourceContract):
    """Host-admitted transfer in a driver-owned workspace, never a host path.

The driver checks session/lease/epoch and expiry at every data boundary.
workspace_id names a contained sandbox workspace, not an absolute path or URI.
"""

    transfer_id: Identifier
    session_id: Identifier
    lease_id: Identifier
    control_epoch: NonNegativeInt
    workspace_id: Identifier
    expires_at: AwareDatetime
    limits: TransferLimits

    def digest(self) -> str:
        from datetime import UTC
        payload = self.model_dump(mode="json")
        payload["expires_at"] = self.expires_at.astimezone(UTC).isoformat()
        if "paths" in payload:
            payload["paths"] = sorted(payload["paths"])
        if "manifest" in payload:
            payload["manifest"] = self.manifest.digest()
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class WorkspaceExportRequest(WorkspaceRequest):
    """Export only explicit regular files, with no glob or directory expansion."""

    paths: Annotated[tuple[WorkspacePath, ...], Field(min_length=1, max_length=1024)]
    entrypoint: WorkspacePath

    @model_validator(mode="after")
    def bounded_selection(self) -> Self:
        if len(self.paths) > self.limits.max_files or len(set(path.casefold() for path in self.paths)) != len(self.paths):
            raise ValueError("export selection exceeds bounds or contains aliases")
        if self.entrypoint not in self.paths:
            raise ValueError("export entrypoint must be explicitly selected")
        return self


class WorkspaceSnapshot(ResourceContract):
    """Verified immutable export snapshot, not a durable publication receipt."""

    request: WorkspaceExportRequest
    snapshot_id: Identifier
    manifest: ArtifactManifest

    @model_validator(mode="after")
    def exact_selection(self) -> Self:
        self.request.limits.check(self.manifest)
        if (set(self.request.paths) != {file.path for file in self.manifest.files}
                or self.request.entrypoint != self.manifest.entrypoint):
            raise ValueError("snapshot differs from authorized file selection")
        return self


class WorkspaceImportRequest(WorkspaceRequest):
    """Materialize a pinned source revision into a fresh workspace target.

The broker reads bytes using its authenticated ArtifactService context. Source
IDs are correlation, not proof of access. An existing target is a conflict;
this operation never replaces a live workspace implicitly.
"""

    source: ArtifactRef
    manifest: ArtifactManifest

    @model_validator(mode="after")
    def bounded_source(self) -> Self:
        self.limits.check(self.manifest)
        return self


class WorkspaceMaterialization(ResourceContract):
    """Complete verified workspace installation, independent of saved ownership."""

    transfer_id: Identifier
    session_id: Identifier
    lease_id: Identifier
    control_epoch: NonNegativeInt
    workspace_id: Identifier
    source: ArtifactRef
    manifest_sha256: Digest
    request_sha256: Digest
    completed_at: AwareDatetime


class WorkspaceTransferStatus(ResourceContract):
    transfer_id: Identifier
    session_id: Identifier
    request_sha256: Digest
    direction: Literal["export", "import"]
    state: Literal["preparing", "ready", "transferring", "complete", "unknown", "aborting", "aborted", "releasing", "released"]
    materialization: WorkspaceMaterialization | None = None
    snapshot: WorkspaceSnapshot | None = None

    @model_validator(mode="after")
    def confirmed_import(self) -> Self:
        if ((self.direction == "export" and self.state in {"complete", "aborting", "aborted"})
                or (self.direction == "import" and self.state in {"releasing", "released"})):
            raise ValueError("transfer state does not apply to this direction")
        expected = self.direction == "import" and self.state == "complete"
        if expected != (self.materialization is not None):
            raise ValueError("completed import requires its materialization receipt")
        receipt = self.materialization
        if receipt is not None and (receipt.transfer_id, receipt.session_id, receipt.request_sha256) != (
                self.transfer_id, self.session_id, self.request_sha256):
            raise ValueError("materialization receipt belongs to another transfer")
        needs_snapshot = self.direction == "export" and self.state in {"ready", "transferring"}
        if needs_snapshot != (self.snapshot is not None):
            raise ValueError("ready export requires its original frozen snapshot")
        if self.snapshot is not None:
            request = self.snapshot.request
            if (request.transfer_id, request.session_id, request.digest()) != (
                    self.transfer_id, self.session_id, self.request_sha256):
                raise ValueError("snapshot belongs to another transfer")
        return self
