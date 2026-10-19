"""Transfer bounds, identity and source/receipt integrity at the SDK boundary."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import inspect
import subprocess
import sys

from pydantic import SecretStr, ValidationError
import pytest

from kestrel_sdk.artifacts import ArtifactFile, ArtifactManifest, ArtifactPage, ArtifactRef, ArtifactRevision, ArtifactProvenance
from kestrel_sdk.computer_use import (ComputerDriverFactory, ComputerSession, DriverConnection,
    EnvironmentCapabilities, EnvironmentProviderFactory, EnvironmentQuote, Key, Navigate, ScreenFrame, SessionRequest)
from kestrel_sdk.resources import ResourceScope
from kestrel_sdk.workspaces import (TransferLimits, WorkspaceExportRequest, WorkspaceImportRequest,
    WorkspaceMaterialization, WorkspaceSnapshot, WorkspaceTransferStatus)

NOW = datetime(2026, 10, 19, tzinfo=UTC)
SCOPE = ResourceScope(tenant_id="tenant", principal_id="user")
FILE = ArtifactFile(path="report.md", media_type="text/markdown", size_bytes=5, sha256=hashlib.sha256(b"hello").hexdigest())
MANIFEST = ArtifactManifest(files=(FILE,), entrypoint=FILE.path)
LIMITS = TransferLimits(max_files=2, max_file_bytes=10, max_total_bytes=20)
REF = ArtifactRef(artifact_id="artifact", revision_id="revision")


def request(**changes):
    return dict(transfer_id="transfer", session_id="session", lease_id="lease", control_epoch=2,
                workspace_id="workspace", expires_at=NOW + timedelta(minutes=1), limits=LIMITS) | changes


def test_export_snapshot_is_exact_and_within_admitted_bounds():
    export = WorkspaceExportRequest(**request(), paths=(FILE.path,), entrypoint=FILE.path)
    snapshot = WorkspaceSnapshot(request=export, snapshot_id="snapshot", manifest=MANIFEST)
    assert WorkspaceSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    other = FILE.model_copy(update={"path": "unselected.md"})
    with pytest.raises(ValidationError, match="selection"):
        WorkspaceSnapshot(request=export, snapshot_id="snapshot",
                          manifest=ArtifactManifest(files=(other,), entrypoint=other.path))
    with pytest.raises(ValidationError, match="limits"):
        WorkspaceSnapshot(request=export.model_copy(update={"limits": TransferLimits(max_files=1, max_file_bytes=4, max_total_bytes=4)}),
                          snapshot_id="snapshot", manifest=MANIFEST)
    for paths in (("../host",), ("/etc/passwd",), ("A.md", "a.md")):
        with pytest.raises(ValidationError):
            WorkspaceExportRequest(**request(), paths=paths, entrypoint=paths[0])
    with pytest.raises(ValidationError, match="entrypoint"):
        WorkspaceExportRequest(**request(), paths=(FILE.path,), entrypoint="missing.md")


def test_transfer_identity_binds_source_epoch_destination_limits_and_canonical_order():
    export = WorkspaceExportRequest(**request(), paths=(FILE.path, "asset.txt"), entrypoint=FILE.path)
    assert export.digest() == export.model_copy(update={"paths": ("asset.txt", FILE.path)}).digest()
    imported = WorkspaceImportRequest(**request(), source=REF, manifest=MANIFEST)
    for changes in ({"control_epoch": 3}, {"workspace_id": "another"}, {"lease_id": "different"},
                    {"source": ArtifactRef(artifact_id="artifact", revision_id="newer")},
                    {"limits": TransferLimits(max_files=1, max_file_bytes=10, max_total_bytes=10)}):
        assert imported.digest() != imported.model_copy(update=changes).digest()
    assert WorkspaceImportRequest.model_validate_json(imported.model_dump_json()).digest() == imported.digest()
    with pytest.raises(ValidationError, match="limits"):
        WorkspaceImportRequest(**request(limits=TransferLimits(max_files=1, max_file_bytes=4, max_total_bytes=4)), source=REF, manifest=MANIFEST)
    with pytest.raises(ValidationError):
        TransferLimits(max_files=True, max_file_bytes=5, max_total_bytes=10)
    with pytest.raises(ValidationError):
        TransferLimits(max_files=1, max_file_bytes=11, max_total_bytes=10)


def test_completed_materialization_needs_correlated_receipt():
    imported = WorkspaceImportRequest(**request(), source=REF, manifest=MANIFEST)
    values = dict(transfer_id="transfer", session_id="session", request_sha256=imported.digest(), direction="import")
    assert WorkspaceTransferStatus(**values, state="unknown").materialization is None
    with pytest.raises(ValidationError, match="requires"):
        WorkspaceTransferStatus(**values, state="complete")
    receipt = WorkspaceMaterialization(transfer_id="transfer", session_id="session", lease_id="lease",
                                      control_epoch=2, workspace_id="workspace", source=REF,
                                      manifest_sha256=MANIFEST.digest(), request_sha256=imported.digest(), completed_at=NOW)
    assert WorkspaceTransferStatus(**values, state="complete", materialization=receipt).materialization == receipt
    for changes in ({"transfer_id": "another"}, {"request_sha256": "f" * 64}):
        with pytest.raises(ValidationError, match="another transfer"):
            WorkspaceTransferStatus(**values, state="complete", materialization=receipt.model_copy(update=changes))
    with pytest.raises(ValidationError):
        WorkspaceTransferStatus(**values, state="unknown", materialization=receipt)


def test_entrypoint_factories_have_an_explicit_private_configuration_channel():
    for factory in (EnvironmentProviderFactory, ComputerDriverFactory):
        parameters = inspect.signature(factory.__call__).parameters
        assert parameters["settings"].kind is inspect.Parameter.KEYWORD_ONLY
    result = subprocess.run([sys.executable, "-c", '''
import sys
import kestrel_sdk.workspaces
for name in ("kestrel_sovereign", "playwright", "docker", "boto3", "azure", "fastapi"):
    assert name not in sys.modules, name
'''], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", ["\x00", "\x1b", "\x7f", "\u202e"])
def test_key_and_url_reject_control_and_display_spoofing(value):
    with pytest.raises(ValidationError):
        Key(key=value)
    with pytest.raises(ValidationError):
        Navigate(url="https://example.com/" + value)


@pytest.mark.parametrize("path", ["CON .txt", "rep\u200dort.md", "rep\u200cort.md"])
def test_artifact_paths_reject_remaining_visual_and_device_aliases(path):
    with pytest.raises(ValidationError):
        ArtifactFile(path=path, media_type=FILE.media_type, size_bytes=FILE.size_bytes, sha256=FILE.sha256)


def test_screen_and_session_require_bounded_visual_capability():
    frame = dict(observation_id="observation", session_id="session", control_epoch=1, observed_at=NOW,
                 image_sha256="a" * 64, media_type="image/png")
    for width, height in ((16385, 1), (1, 16385), (4097, 4096)):
        with pytest.raises(ValidationError):
            ScreenFrame(**frame, width=width, height=height)
    assert ScreenFrame(**frame, width=4096, height=4096).width == 4096
    with pytest.raises(ValidationError, match="screenshot"):
        SessionRequest(session_id="session", scope=SCOPE, lease_id="lease", surface="browser",
                       required_capabilities=frozenset({"click"}), expires_at=NOW)
    for state, controller in (("ready", "agent"), ("paused", "human"), ("paused", "none"), ("closed", "none")):
        assert ComputerSession(session_id="session", scope=SCOPE, lease_id="lease", driver_id="driver", surface="browser",
                               capabilities=frozenset({"screenshot"}), state=state, controller=controller,
                               control_epoch=1, expires_at=NOW).scope.agent_id is None


def test_revision_cannot_parent_itself_and_pages_are_bounded():
    values = dict(ref=REF, scope=SCOPE, title="Report", manifest=MANIFEST,
                  provenance=ArtifactProvenance(), committed_at=NOW)
    with pytest.raises(ValidationError, match="own parent"):
        ArtifactRevision(**values, parent_revision_id=REF.revision_id)
    revision = ArtifactRevision(**values)
    assert len(ArtifactPage(items=(revision,) * 100).items) == 100
    with pytest.raises(ValidationError):
        ArtifactPage(items=(revision,) * 101)
    with pytest.raises(ValidationError):
        ArtifactPage(items=(), next_cursor="")


def test_connections_and_quote_capabilities_cannot_disagree():
    for endpoint, credential in (("", "secret"), ("https://driver", "")):
        with pytest.raises(ValidationError):
            DriverConnection(lease_id="lease", driver_id="driver", endpoint=SecretStr(endpoint),
                             credential=SecretStr(credential), expires_at=NOW)
    caps = EnvironmentCapabilities(provider_id="docker", location="local", surfaces=frozenset({"browser"}), operations=frozenset({"screenshot"}))
    quote = dict(quote_id="quote", request_id="request", request_sha256="a" * 64, scope=SCOPE,
                 provider_id="docker", location="local", expires_at=NOW, hourly_cost_usd=Decimal("0"),
                 estimated_total_cost_usd=Decimal("0"), capabilities=caps)
    assert EnvironmentQuote(**quote).capabilities == caps
    for changes in ({"provider_id": "remote"}, {"location": "remote"}):
        with pytest.raises(ValidationError, match="different providers"):
            EnvironmentQuote(**(quote | changes))
