"""Consumer/epoch custody and bounded manual input, without a runtime adapter."""

from datetime import UTC, datetime, timedelta, timezone
import hashlib
import inspect
import subprocess
import sys

from pydantic import SecretBytes, ValidationError
import pytest

import kestrel_sdk.computer_use as public
from kestrel_sdk.computer_use import (
    ActionReceipt, ActionRequest, Click, ComputerDriver, ComputerSession,
    HumanActionReceipt, HumanActionRequest, HumanControl, HumanControlLease,
    HumanControlProvider, HumanControlRequest, HumanControlRevocation,
    HumanScreenObservation, Key, Navigate, ScreenFrame, ScreenObservation,
    Scroll, TypeText, Wait,
)
from kestrel_sdk.resources import ResourceScope

NOW = datetime(2026, 10, 26, tzinfo=UTC)
SCOPE = ResourceScope(tenant_id="tenant", principal_id="operator", agent_id="agent")
CAPABILITIES = frozenset({"screenshot", "click", "type", "key", "scroll", "wait", "navigate"})
CHECK = NOW + timedelta(seconds=3)
PIXELS = b"private human pixels"


def session(**changes):
    return ComputerSession(**(dict(
        session_id="session", scope=SCOPE, lease_id="resource", driver_id="driver",
        surface="browser", capabilities=CAPABILITIES | {"takeover"},
        state="paused", controller="human", control_epoch=2,
        expires_at=NOW + timedelta(minutes=2),
    ) | changes))


def request(**changes):
    return HumanControlRequest(**(dict(
        control_id="control", scope=SCOPE, session_id="session", lease_id="resource",
        driver_id="driver", consumer_id="authenticated-consumer", control_epoch=2,
        issued_at=NOW, expires_at=NOW + timedelta(minutes=1),
        capabilities=CAPABILITIES, max_frame_age_seconds=10,
    ) | changes))


def lease(**changes):
    original = changes.pop("request", request())
    return HumanControlLease(**(dict(
        request=original, request_sha256=original.digest(), state="active",
        accepted_at=NOW + timedelta(seconds=1),
    ) | changes))


def observation(**changes):
    frame = ScreenFrame(**(dict(
        observation_id="human-observation", session_id="session", control_epoch=2,
        observed_at=NOW + timedelta(seconds=2), width=100, height=80,
        image_sha256=hashlib.sha256(PIXELS).hexdigest(), media_type="image/png",
    ) | changes))
    return HumanScreenObservation(
        control_id="control", consumer_id="authenticated-consumer",
        observation=ScreenObservation(frame=frame, image=SecretBytes(PIXELS)),
    )


def action(**changes):
    payload = ActionRequest(**(dict(
        action_id="human-input", session_id="session", observation_id="human-observation",
        control_epoch=2, not_after=NOW + timedelta(seconds=10), action=Click(x=10, y=20),
    ) | changes))
    return HumanActionRequest(
        control_id="control", consumer_id="authenticated-consumer", action=payload,
    )


def revocation(**changes):
    return HumanControlRevocation(**(dict(
        request_id="revoke", control_id="control", session_id="session",
        consumer_id="authenticated-consumer", expected_control_epoch=2,
    ) | changes))


def test_public_optional_protocol_does_not_extend_or_replace_agent_driver():
    expected = {
        "HumanActionReceipt", "HumanActionRequest", "HumanCapability", "HumanControl",
        "HumanControlLease", "HumanControlProvider", "HumanControlRequest",
        "HumanControlRevocation", "HumanScreenObservation",
    }
    assert expected <= set(public.__all__)
    for name in expected:
        assert getattr(public, name) is getattr(sys.modules[HumanControl.__module__], name)
    assert "human_control" not in ComputerDriver.__dict__
    assert set(name for name in HumanControl.__dict__ if not name.startswith("_")) == {
        "open", "status", "observe", "act", "action_status", "revoke",
    }
    assert inspect.signature(HumanControlProvider.human_control).return_annotation == "HumanControl"

    class WithHuman:
        def human_control(self):
            return self

        async def open(self, session, request): ...
        async def status(self, session, request): ...
        async def observe(self, session, lease): ...
        async def act(self, session, lease, request): ...
        async def action_status(self, session, lease, action_id): ...
        async def revoke(self, session, request): ...

    assert isinstance(WithHuman(), HumanControlProvider)
    assert isinstance(WithHuman().human_control(), HumanControl)
    assert not isinstance(object(), HumanControlProvider)
    assert not isinstance(object(), HumanControl)


def test_import_blocks_browser_cloud_host_and_network_runtime_dependencies():
    script = '''
import importlib.abc
import sys
blocked = {"kestrel_sovereign", "playwright", "docker", "boto3", "azure", "fastapi", "httpx", "httpx2", "requests"}
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in blocked:
            raise AssertionError("unexpected runtime import: " + fullname)
sys.meta_path.insert(0, Block())
from kestrel_sdk.computer_use import HumanControl, HumanControlProvider, HumanControlRequest
assert not blocked.intersection(sys.modules)
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("changes", [
    {"schema_version": 2}, {"schema_version": True}, {"unknown": "no"},
    {"control_epoch": True}, {"max_frame_age_seconds": True},
    {"max_frame_age_seconds": 0}, {"max_frame_age_seconds": 61},
    {"control_id": "../control"}, {"consumer_id": ""},
    {"capabilities": frozenset({"screenshot", "exec"})},
    {"capabilities": frozenset({"screenshot", "files"})},
    {"capabilities": frozenset({"click"})},
    {"issued_at": NOW.replace(tzinfo=None)}, {"expires_at": NOW},
])
def test_requests_are_strict_immutable_and_have_no_unbounded_surfaces(changes):
    with pytest.raises(ValidationError):
        request(**changes)


def test_original_request_digest_canonicalizes_set_and_timezone_and_binds_every_fact():
    original = request()
    offset = timezone(timedelta(hours=-5))
    equivalent = request(
        capabilities=frozenset(reversed(sorted(CAPABILITIES))),
        issued_at=NOW.astimezone(offset),
        expires_at=original.expires_at.astimezone(offset),
    )
    assert original.digest() == equivalent.digest()
    assert HumanControlRequest.model_validate_json(original.model_dump_json()).digest() == original.digest()
    for changes in (
        {"control_id": "another"}, {"consumer_id": "other-consumer"},
        {"scope": ResourceScope(tenant_id="tenant", principal_id="operator", agent_id=None)},
        {"session_id": "another"}, {"lease_id": "another"}, {"driver_id": "another"},
        {"control_epoch": 3}, {"issued_at": NOW - timedelta(seconds=1)},
        {"expires_at": NOW + timedelta(seconds=61)},
        {"capabilities": frozenset({"screenshot"})}, {"max_frame_age_seconds": 9},
    ):
        changed = request(**changes)
        assert changed.digest() != original.digest(), changes
        with pytest.raises(ValueError, match="binding"):
            lease().require_request(changed)
    lease().require_request(equivalent)
    with pytest.raises(ValidationError):
        original.consumer_id = "replacement"


@pytest.mark.parametrize("changes", [
    {"scope": ResourceScope(tenant_id="other", principal_id="operator", agent_id="agent")},
    {"scope": ResourceScope(tenant_id="tenant", principal_id="other", agent_id="agent")},
    {"scope": ResourceScope(tenant_id="tenant", principal_id="operator", agent_id=None)},
    {"session_id": "other"}, {"lease_id": "other"}, {"driver_id": "other"},
    {"control_epoch": 3}, {"state": "ready", "controller": "agent"},
    {"controller": "none"}, {"state": "closed", "controller": "none"},
    {"capabilities": CAPABILITIES},
    {"capabilities": frozenset({"screenshot", "takeover"})},
    {"expires_at": NOW + timedelta(seconds=59)},
])
def test_input_cannot_cross_owner_session_resource_driver_epoch_state_or_capability(changes):
    with pytest.raises(ValueError):
        action().require_lease(lease(), session(**changes), observation(), at=CHECK)


@pytest.mark.parametrize("at", [NOW - timedelta(microseconds=1), NOW + timedelta(minutes=1), NOW.replace(tzinfo=None)])
def test_live_lease_requires_trusted_current_time_and_fixed_expiry(at):
    with pytest.raises(ValueError):
        lease().require_active(session(), at=at)


@pytest.mark.parametrize("changes", [
    {"request_sha256": "f" * 64},
    {"accepted_at": NOW - timedelta(microseconds=1)},
    {"accepted_at": NOW + timedelta(minutes=1)},
    {"state": "revoked"}, {"state": "active", "revoked_at": CHECK},
    {"state": "unknown", "revoked_at": CHECK},
    {"state": "revoked", "revoked_at": NOW},
])
def test_lease_requires_original_acceptance_and_explicit_retirement_facts(changes):
    with pytest.raises(ValidationError):
        lease(**changes)


@pytest.mark.parametrize("state", ["unknown", "revoking", "revoked"])
def test_uncertain_or_retired_control_never_authorizes_pixels_or_input(state):
    retired = lease(state=state, **({"revoked_at": CHECK} if state == "revoked" else {}))
    with pytest.raises(ValueError, match="not active"):
        observation().require_lease(retired, session(), at=CHECK)
    with pytest.raises(ValueError, match="not active"):
        action().require_lease(retired, session(), observation(), at=CHECK)
    assert HumanControlLease.model_validate_json(retired.model_dump_json()) == retired


@pytest.mark.parametrize("changes", [
    {"session_id": "other"}, {"control_epoch": 3},
    {"observed_at": NOW}, {"observed_at": CHECK + timedelta(microseconds=1)},
    {"observed_at": NOW + timedelta(seconds=1)},
])
def test_human_frame_requires_accepted_binding_and_freshness(changes):
    at = NOW + timedelta(seconds=11) if changes == {"observed_at": NOW + timedelta(seconds=1)} else CHECK
    with pytest.raises(ValueError):
        observation(**changes).require_lease(lease(), session(), at=at)


@pytest.mark.parametrize("field", ["consumer_id", "control_id"])
def test_frames_and_input_cannot_borrow_another_consumers_lease(field):
    for value in (observation(), action()):
        changed = value.model_copy(update={field: "other"})
        with pytest.raises(ValueError, match="consumer"):
            if isinstance(changed, HumanActionRequest):
                changed.require_lease(lease(), session(), observation(), at=CHECK)
            else:
                changed.require_lease(lease(), session(), at=CHECK)


@pytest.mark.parametrize("changes", [
    {"session_id": "other"}, {"control_epoch": 3}, {"observation_id": "agent-frame"},
    {"not_after": CHECK}, {"not_after": NOW + timedelta(seconds=12, microseconds=1)},
    {"action": Click(x=100, y=20)}, {"action": Click(x=10, y=80)},
])
def test_manual_input_checks_current_frame_bounds_and_deadline(changes):
    with pytest.raises(ValueError):
        action(**changes).require_lease(lease(), session(), observation(), at=CHECK)


def test_manual_input_cannot_outlive_fixed_lease_or_use_unleased_action():
    short = lease(request=request(expires_at=NOW + timedelta(seconds=11)))
    with pytest.raises(ValueError, match="deadline"):
        action(not_after=NOW + timedelta(seconds=12)).require_lease(short, session(), observation(), at=CHECK)
    limited = lease(request=request(capabilities=frozenset({"screenshot"})))
    with pytest.raises(ValueError, match="capabilities"):
        action().require_lease(limited, session(), observation(), at=CHECK)
    with pytest.raises(ValueError, match="not active"):
        lease(accepted_at=NOW + timedelta(seconds=4)).require_active(session(), at=CHECK)


@pytest.mark.parametrize("payload", [
    Click(x=99, y=79), TypeText(text="private typed value"), Key(key="Enter"),
    Scroll(delta_y=80), Wait(milliseconds=1), Navigate(url="https://example.test/private"),
])
def test_canonical_payloads_are_supported_without_using_agent_act(payload):
    original = action(action=payload)
    original.require_lease(lease(), session(), observation(), at=CHECK)
    encoded = original.model_dump_json()
    assert HumanActionRequest.model_validate_json(encoded) == original
    assert payload.kind in CAPABILITIES
    offset = timezone(timedelta(hours=3))
    assert original.digest() == original.model_copy(update={
        "action": original.action.model_copy(update={"not_after": original.action.not_after.astimezone(offset)}),
    }).digest()


def test_action_identity_binds_private_payload_consumer_and_canonical_request():
    original = action(action=TypeText(text="private typed value"))
    for changes in (
        {"control_id": "other"}, {"consumer_id": "other"},
        {"action": original.action.model_copy(update={"action_id": "other"})},
        {"action": original.action.model_copy(update={"action": TypeText(text="changed")})},
        {"action": original.action.model_copy(update={"not_after": NOW + timedelta(seconds=9)})},
    ):
        assert original.model_copy(update=changes).digest() != original.digest()
    assert "private typed value" not in repr(original)
    private = observation()
    assert "image" not in private.model_dump()["observation"]
    assert "image" not in private.model_dump_json().replace("image_sha256", "digest").replace("image/png", "png")
    assert "private human pixels" not in repr(private)
    assert private.observation.image.get_secret_value() == PIXELS


@pytest.mark.parametrize("changes", [
    {"action_id": "other"}, {"session_id": "other"},
    {"observation_id": "agent-frame"}, {"control_epoch": 3},
])
def test_original_receipts_do_not_cross_input_identity(changes):
    original = action()
    receipt = HumanActionReceipt(
        control_id=original.control_id, consumer_id=original.consumer_id,
        receipt=ActionReceipt(**(dict(
            action_id="human-input", session_id="session", observation_id="human-observation",
            control_epoch=2, outcome="unknown", recorded_at=CHECK,
        ) | changes)),
    )
    with pytest.raises(ValueError, match="original action"):
        receipt.require_request(original)


@pytest.mark.parametrize("outcome", ["completed", "denied", "unknown"])
def test_reconciliation_preserves_original_receipt_including_uncertain_input(outcome):
    original = action()
    receipt = HumanActionReceipt(
        control_id="control", consumer_id="authenticated-consumer",
        receipt=ActionReceipt(
            action_id="human-input", session_id="session", observation_id="human-observation",
            control_epoch=2, outcome=outcome, recorded_at=CHECK,
        ),
    )
    receipt.require_request(original)
    assert HumanActionReceipt.model_validate_json(receipt.model_dump_json()) == receipt
    for field in ("consumer_id", "control_id"):
        with pytest.raises(ValueError, match="original action"):
            receipt.model_copy(update={field: "other"}).require_request(original)


def test_retirement_keeps_original_epoch_after_expiry_and_handback():
    original = lease(state="unknown")
    advanced = session(state="ready", controller="agent", control_epoch=3, expires_at=NOW - timedelta(seconds=1))
    revocation().require_target(original, advanced)
    with pytest.raises(ValueError):
        original.require_active(advanced, at=NOW + timedelta(minutes=3))
    with pytest.raises(ValueError, match="original consumer"):
        revocation(expected_control_epoch=3).require_target(original, advanced)


def test_retirement_digest_binds_original_target_and_idempotency_id():
    original = revocation()
    assert HumanControlRevocation.model_validate_json(original.model_dump_json()).digest() == original.digest()
    for changes in (
        {"request_id": "other"}, {"control_id": "other"}, {"session_id": "other"},
        {"consumer_id": "other"}, {"expected_control_epoch": 3},
    ):
        assert revocation(**changes).digest() != original.digest()


def test_freshness_checks_do_not_overflow_near_datetime_limit():
    end = datetime.max.replace(tzinfo=UTC)
    start = end - timedelta(seconds=20)
    current = end - timedelta(seconds=1)
    original = request(issued_at=start, expires_at=end, max_frame_age_seconds=20)
    admitted = session(expires_at=end)
    active = lease(request=original, accepted_at=start)
    frame = observation(observed_at=end - timedelta(seconds=2))
    frame.require_lease(active, admitted, at=current)
    action(not_after=end).require_lease(active, admitted, frame, at=current)


@pytest.mark.parametrize("changes", [
    {"control_id": "other"}, {"session_id": "other"},
    {"consumer_id": "other"}, {"expected_control_epoch": 1},
])
def test_retirement_targets_exact_original_consumer_lease(changes):
    with pytest.raises(ValueError):
        revocation(**changes).require_target(lease(), session())


@pytest.mark.parametrize("changes", [
    {"scope": ResourceScope(tenant_id="other", principal_id="operator", agent_id="agent")},
    {"scope": ResourceScope(tenant_id="tenant", principal_id="other", agent_id="agent")},
    {"scope": ResourceScope(tenant_id="tenant", principal_id="operator", agent_id=None)},
    {"session_id": "other"}, {"lease_id": "other"}, {"driver_id": "other"},
])
def test_cleanup_never_borrows_another_owners_resource_custody(changes):
    with pytest.raises(ValueError, match="owner/session"):
        revocation().require_target(lease(), session(**changes))
