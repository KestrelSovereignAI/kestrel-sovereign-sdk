"""Logical deliverables, immutable content manifests and publication receipts.

IDs and hashes are not access tokens. A receipt is issued only after the host
has accepted durable bytes and atomically committed authorized revision state.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, AwareDatetime, Field, field_validator, model_validator

from kestrel_sdk._validation import browser_safe_string, non_empty_text

from kestrel_sdk.resources import (
    Identifier, NonNegativeInt, ResourceContract, ResourceScope,
)

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
MediaType = Annotated[
    str, Field(max_length=255, pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
]
Title = Annotated[str, AfterValidator(lambda value: non_empty_text(value, "title"))]
_RESERVED_NAMES = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in "123456789¹²³"
}


class ArtifactRef(ResourceContract):
    """A pinned revision; neither an originating run nor a storage URL."""

    artifact_id: Identifier
    revision_id: Identifier


class ArtifactFile(ResourceContract):
    """One regular file, addressed by a logical bundle path and verified bytes."""

    path: Annotated[str, Field(min_length=1, max_length=1024)]
    sha256: Digest
    size_bytes: NonNegativeInt
    media_type: MediaType

    @field_validator("path")
    @classmethod
    def contained_path(cls, value: str) -> str:
        browser_safe_string(value, "artifact path", max_length=1024, allow_empty=False)
        parts = value.split("/")
        if (
            PurePosixPath(value).is_absolute()
            or "\\" in value
            or any(char in value for char in ':*?"<>|')
            or any(part in {"", ".", ".."} for part in parts)
            or any(part != part.strip() or part.endswith(".") for part in parts)
            or any(part.split(".")[0].rstrip(" ").casefold() in _RESERVED_NAMES for part in parts)
            or "\u200c" in value or "\u200d" in value
            or unicodedata.normalize("NFC", value) != value
        ):
            raise ValueError("artifact path must be a contained relative POSIX path")
        return value


class ArtifactManifest(ResourceContract):
    """Required source files; derived representations are separately revision-bound.

Limits here bound the contract. Hosts may impose lower byte/file quotas and
must verify regular-file containment, hashes and size on actual ingestion.
"""

    files: Annotated[tuple[ArtifactFile, ...], Field(min_length=1, max_length=1024)]
    entrypoint: str

    @model_validator(mode="after")
    def unique_complete_paths(self) -> Self:
        paths = {item.path.casefold() for item in self.files}
        if len(paths) != len(self.files):
            raise ValueError("artifact manifest contains duplicate paths")
        if self.entrypoint not in {item.path for item in self.files}:
            raise ValueError("artifact entrypoint must identify a manifest file")
        for path in paths:
            parents = PurePosixPath(path).parents
            if any(str(parent) in paths for parent in parents):
                raise ValueError("artifact path cannot be both a file and directory")
        return self

    def digest(self) -> str:
        """Stable source-manifest identity, independent of producer file order."""

        payload = self.model_dump(mode="json")
        payload["files"] = sorted(payload["files"], key=lambda item: item["path"])
        content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(content).hexdigest()


class ArtifactProvenance(ResourceContract):
    """Optional attribution; never authority and never a cascading owner link."""

    session_id: Identifier | None = None
    run_id: Identifier | None = None
    tool_name: Identifier | None = None


class ArtifactCommit(ResourceContract):
    """Publish staged content using compare-and-swap, not a blind overwrite.

A new artifact omits artifact_id and expected_revision_id. An update requires
both. The service binds the idempotency key to the exact request and owner;
reusing a key with different content is a conflict, including across restarts.
"""

    idempotency_key: Identifier
    artifact_id: Identifier | None = None
    expected_revision_id: Identifier | None = None
    title: Title
    manifest: ArtifactManifest
    provenance: ArtifactProvenance = Field(default_factory=ArtifactProvenance)

    @model_validator(mode="after")
    def compare_and_swap(self) -> Self:
        if (self.artifact_id is None) != (self.expected_revision_id is None):
            raise ValueError("artifact updates require an explicit expected revision")
        return self

    def digest(self) -> str:
        """Canonical publication identity including title, CAS target and lineage."""
        payload = self.model_dump(mode="json")
        payload["manifest"] = self.manifest.digest()
        content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(content).hexdigest()


class ArtifactRevision(ResourceContract):
    ref: ArtifactRef
    scope: ResourceScope
    parent_revision_id: Identifier | None = None
    title: Title
    manifest: ArtifactManifest
    provenance: ArtifactProvenance
    committed_at: AwareDatetime

    @model_validator(mode="after")
    def distinct_parent(self) -> Self:
        if self.parent_revision_id == self.ref.revision_id:
            raise ValueError("artifact revision cannot be its own parent")
        return self


class ArtifactRepresentation(ResourceContract):
    """A derived view of an exact source revision, never a mutable replacement."""

    representation_id: Identifier
    source: ArtifactRef
    kind: Literal["pdf", "thumbnail", "text", "preview", "export"]
    file: ArtifactFile


class ArtifactPage(ResourceContract):
    """Bounded, authorized list/history page; cursors convey no authority."""

    items: Annotated[tuple[ArtifactRevision, ...], Field(max_length=100)]
    next_cursor: Annotated[str, Field(min_length=1, max_length=2048)] | None = None


class PublicationReceipt(ResourceContract):
    """Durable acceptance, independent of subsequent compute cleanup.

A receipt is historical evidence, not permission to read or a promise against
future policy erasure. Every read still rechecks current authority/lifecycle.
"""

    ref: ArtifactRef
    scope: ResourceScope
    idempotency_key: Identifier
    manifest_sha256: Digest
    request_sha256: Digest
    committed_at: AwareDatetime
    storage_id: Identifier


class ArtifactLink(ResourceContract):
    ref: ArtifactRef
    context_kind: Literal["conversation", "project", "task", "run"]
    context_id: Identifier


class ExternalArtifactReference(ResourceContract):
    """Provider-owned object, explicitly not a managed durable snapshot."""

    provider_id: Identifier
    connection_id: Identifier
    object_id: Annotated[str, Field(min_length=1, max_length=2048)]
    capabilities: frozenset[Literal["read", "edit", "export"]]
