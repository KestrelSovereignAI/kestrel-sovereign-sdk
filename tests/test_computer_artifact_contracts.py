"""Untrusted boundary and cross-producer invariants, without runtime imports."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import subprocess
import sys

from pydantic import SecretBytes, SecretStr, ValidationError
import pytest

from kestrel_sdk.artifacts import (
    ArtifactCommit, ArtifactFile, ArtifactManifest, ArtifactProvenance, ArtifactRef,
)
from kestrel_sdk.computer_use import (
    ActionRequest, ComputerSession, DriverConnection, EnvironmentLease,
    EnvironmentRequest, ScreenFrame, ScreenObservation, TypeText,
)
from kestrel_sdk.resources import ResourceScope


NOW = datetime(2026, 10, 19, tzinfo=UTC)
SCOPE = ResourceScope(tenant_id="tenant", principal_id="user", agent_id="agent")


def environment(**changes):
    values = dict(
        request_id="request", scope=SCOPE, provider_id="docker", profile_id="isolated",
        image="registry/browser@sha256:" + "a" * 64, surface="browser",
        required_capabilities=frozenset({"screenshot", "click"}), location="local",
        requested_at=NOW, expires_at=NOW + timedelta(minutes=5), idle_ttl_seconds=60,
        ready_timeout_seconds=120, max_hourly_cost_usd=Decimal("0"),
        max_total_cost_usd=Decimal("0"),
    )
    return EnvironmentRequest(**(values | changes))


def source(path="report.md", content=b"hello"):
    return ArtifactFile(path=path, sha256=hashlib.sha256(content).hexdigest(),
                        size_bytes=len(content), media_type="text/markdown")


def test_wire_round_trip_is_strict_frozen_and_provider_neutral():
    request = environment()
    assert EnvironmentRequest.model_validate_json(request.model_dump_json()) == request
    with pytest.raises(ValidationError):
        request.provider_id = "runpod"
    payload = json.loads(request.model_dump_json())
    for key, value in [("schema_version", 2), ("schema_version", True),
                       ("idle_ttl_seconds", True), ("idle_ttl_seconds", "60"),
                       ("runtime", "ollama")]:
        with pytest.raises(ValidationError):
            EnvironmentRequest.model_validate_json(json.dumps(payload | {key: value}))


def test_quote_digest_binds_constraints_without_set_or_timezone_ambiguity():
    request = environment()
    assert request.digest() == environment(required_capabilities=frozenset({"click", "screenshot"})).digest()
    assert request.digest() != environment(max_total_cost_usd=Decimal("1")).digest()
    assert request.digest() != environment(scope=ResourceScope(
        tenant_id="another", principal_id="user", agent_id="agent")).digest()
    from datetime import timezone
    assert request.digest() == environment(
        requested_at=NOW.astimezone(timezone(timedelta(hours=2))),
        expires_at=(NOW + timedelta(minutes=5)).astimezone(timezone(timedelta(hours=2))),
    ).digest()


@pytest.mark.parametrize("changes", [
    {"expires_at": NOW}, {"idle_ttl_seconds": 301}, {"ready_timeout_seconds": 301},
    {"requested_at": NOW.replace(tzinfo=None)}, {"image": "browser:latest"},
    {"max_total_cost_usd": Decimal("NaN")}, {"max_total_cost_usd": Decimal("Infinity")},
    {"max_hourly_cost_usd": Decimal("-1")}, {"max_total_cost_usd": 1.0},
    {"required_capabilities": frozenset({"click"})},
    {"required_capabilities": frozenset({"screenshot", "silent-host-access"})},
])
def test_environment_constraints_cannot_be_silently_weakened(changes):
    with pytest.raises(ValidationError):
        environment(**changes)


def test_failed_or_lost_environment_is_not_physical_release():
    values = dict(lease_id="lease", request_id="request", scope=SCOPE,
                  provider_id="docker", location="local", expires_at=NOW,
                  provider_resource_id="container")
    assert EnvironmentLease(**values, state="unknown").actual_cost_usd is None
    with pytest.raises(ValidationError):
        EnvironmentLease(**values, state="released")
    with pytest.raises(ValidationError):
        EnvironmentLease(**values, state="failed", released_at=NOW)
    assert EnvironmentLease(**values, state="released", released_at=NOW).released_at == NOW


@pytest.mark.parametrize("state,controller", [
    ("paused", "agent"), ("unknown", "agent"), ("closed", "human"), ("ready", "human"),
])
def test_control_state_cannot_claim_agent_authority_after_handoff(state, controller):
    with pytest.raises(ValidationError):
        ComputerSession(session_id="session", scope=SCOPE, lease_id="lease",
                        driver_id="browser", surface="browser", capabilities=frozenset(),
                        state=state, controller=controller, control_epoch=1, expires_at=NOW)


def test_action_envelope_rejects_invented_or_malformed_actions():
    envelope = dict(action_id="action", session_id="session", observation_id="frame",
                    control_epoch=2)
    request = ActionRequest(**envelope, action=TypeText(text="private input"))
    assert "private input" not in repr(request)
    assert ActionRequest.model_validate_json(request.model_dump_json()) == request
    for action in [dict(kind="shell", command="rm"), dict(kind="click", x=-1, y=1),
                   dict(kind="click", x=True, y=1), dict(kind="type", text=""),
                   dict(kind="wait", milliseconds=10001)]:
        with pytest.raises(ValidationError):
            ActionRequest.model_validate(envelope | {"action": action})


def test_secrets_and_pixels_are_not_generic_serializable_metadata():
    connection = DriverConnection(lease_id="lease", driver_id="driver",
                                  endpoint=SecretStr("https://private-host"),
                                  credential=SecretStr("secret-value"), expires_at=NOW)
    observation = ScreenObservation(frame=ScreenFrame(
        observation_id="frame", session_id="session", control_epoch=1,
        observed_at=NOW, width=640, height=480,
        image_sha256=hashlib.sha256(b"private pixels").hexdigest(),
        media_type="image/png"), image=SecretBytes(b"private pixels"))
    for record in (connection, observation):
        text = repr(record) + record.model_dump_json()
        assert not any(value in text for value in ("private-host", "secret-value", "private pixels"))
    assert observation.image.get_secret_value() == b"private pixels"
    with pytest.raises(ValidationError, match="digest"):
        ScreenObservation(frame=observation.frame, image=SecretBytes(b"wrong frame"))


@pytest.mark.parametrize("path", ["/tmp/a", "../a", "a/../b", "a//b", "./a", "a/",
                                  "a\\b", "C:foo", "a\x00b", "a\nb"])
def test_bundle_paths_cannot_escape_materialization(path):
    with pytest.raises(ValidationError):
        source(path)


def test_manifest_is_complete_unambiguous_and_order_independent():
    one, two = source(), source("assets/chart.txt")
    first = ArtifactManifest(files=(one, two), entrypoint=one.path)
    second = ArtifactManifest(files=(two, one), entrypoint=one.path)
    assert first.digest() == second.digest()
    for files, entrypoint in [((one, one), one.path), ((one,), "missing"),
                              ((source("a"), source("a/b")), "a")]:
        with pytest.raises(ValidationError):
            ArtifactManifest(files=files, entrypoint=entrypoint)
    assert first.digest() != ArtifactManifest(files=(source(content=b"edit"), two),
                                             entrypoint=one.path).digest()


def test_artifact_creation_and_edits_have_no_run_or_machine_dependency():
    manifest = ArtifactManifest(files=(source(),), entrypoint="report.md")
    new = ArtifactCommit(idempotency_key="upload", title="Report", manifest=manifest)
    assert new.provenance == ArtifactProvenance()
    updated = ArtifactCommit(idempotency_key="edit", artifact_id="artifact",
                             expected_revision_id="rev1", title="Report", manifest=manifest)
    assert updated.artifact_id == "artifact"
    for changes in ({"artifact_id": "artifact"}, {"expected_revision_id": "rev1"}):
        with pytest.raises(ValidationError):
            ArtifactCommit(idempotency_key="bad", title="Report", manifest=manifest, **changes)
    assert ArtifactRef(artifact_id="artifact", revision_id="rev2").revision_id == "rev2"


def test_contract_imports_do_not_pull_runtime_or_cloud_dependencies():
    result = subprocess.run([sys.executable, "-c", '''
import sys
import kestrel_sdk.computer_use
import kestrel_sdk.artifacts
for name in ("kestrel_sovereign", "playwright", "docker", "boto3", "azure", "fastapi"):
    assert name not in sys.modules, name
'''], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
