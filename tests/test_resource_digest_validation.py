"""Shared UTC digest normalization handles offset range failures consistently."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kestrel_sdk._validation import canonical_utc_datetime
from kestrel_sdk.computer_use import EnvironmentRequest
from kestrel_sdk.resources import ResourceScope
from kestrel_sdk.workspaces import TransferLimits, WorkspaceExportRequest


def test_normalization_preserves_existing_aware_utc_wire_identity():
    now = datetime(2026, 10, 26, 12, 1, 2, 345678, tzinfo=UTC)
    assert canonical_utc_datetime(now) == now.isoformat()
    assert canonical_utc_datetime(now.astimezone(timezone(timedelta(hours=-5)))) == now.isoformat()
    with pytest.raises(ValueError):
        canonical_utc_datetime(now.replace(tzinfo=None))


@pytest.mark.parametrize("edge", ["lower", "upper"])
def test_environment_and_workspace_digest_offsets_never_escape_as_raw_overflow(edge):
    if edge == "upper":
        end = datetime.max.replace(tzinfo=timezone(timedelta(hours=-5)))
        start = end - timedelta(minutes=1)
    else:
        start = datetime.min.replace(tzinfo=timezone(timedelta(hours=5)))
        end = start + timedelta(minutes=1)
    environment = EnvironmentRequest(
        request_id="request", scope=ResourceScope(tenant_id="tenant", principal_id="operator"),
        provider_id="provider", profile_id="profile", image="worker@sha256:" + "a" * 64,
        surface="browser", location="local", required_capabilities=frozenset({"screenshot"}),
        requested_at=start, expires_at=end, idle_ttl_seconds=1, ready_timeout_seconds=1,
        max_hourly_cost_usd=Decimal(0), max_total_cost_usd=Decimal(0),
    )
    workspace = WorkspaceExportRequest(
        transfer_id="transfer", session_id="session", lease_id="resource", control_epoch=2,
        workspace_id="workspace", expires_at=end, limits=TransferLimits(max_files=1, max_file_bytes=1, max_total_bytes=1),
        paths=("file.txt",), entrypoint="file.txt",
    )
    for request in (environment, workspace):
        with pytest.raises(ValueError, match="UTC range"):
            request.digest()
