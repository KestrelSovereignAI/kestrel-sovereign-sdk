"""Contract tests for provider-neutral remote inference leases."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr

from kestrel_sdk.llm import (
    INFERENCE_LEASE_PROVIDER_ENTRY_POINT_GROUP,
    InferenceLease,
    InferenceLeaseConstraintError,
    InferenceLeaseFailure,
    InferenceLeaseOwnershipError,
    InferenceLeaseProvider,
    InferenceLeaseQuote,
    InferenceLeaseRequest,
    InferenceLeaseState,
    InferencePrivacy,
    InferenceProviderCapability,
    InferenceRoute,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
NAIVE_NOW = NOW.replace(tzinfo=None)


def make_request(**overrides) -> InferenceLeaseRequest:
    values = {
        "request_id": "request-123",
        "owner_id": "did:kestrel:kite",
        "model": "qwen3:8b",
        "runtime": "ollama",
        "max_hourly_cost_usd": Decimal("0.75"),
        "max_total_cost_usd": Decimal("0.50"),
        "privacy": InferencePrivacy.AUTHENTICATED_ENDPOINT,
        "capabilities": ("chat", "tools"),
        "allowed_regions": ("us-ks-2",),
        "expected_concurrency": 2,
        "expected_session_seconds": 1800,
        "idle_ttl_seconds": 300,
        "ready_deadline_seconds": 600,
        "requested_at": NOW,
        "metadata": {"purpose": "kite-dogfood", "labels": ["private"]},
    }
    values.update(overrides)
    return InferenceLeaseRequest(**values)


def make_quote(**overrides) -> InferenceLeaseQuote:
    values = {
        "quote_id": "quote-123",
        "request_id": "request-123",
        "provider_name": "runpod",
        "runtime": "ollama",
        "region": "us-ks-2",
        "privacy": InferencePrivacy.AUTHENTICATED_ENDPOINT,
        "hourly_cost_usd": Decimal("0.57"),
        "estimated_total_cost_usd": Decimal("0.29"),
        "estimated_ready_seconds": 180,
        "expires_at": NOW + timedelta(minutes=5),
        "metadata": {"gpu_class": "rtx-pro-4000"},
    }
    values.update(overrides)
    return InferenceLeaseQuote(**values)


def make_route() -> InferenceRoute:
    return InferenceRoute(
        endpoint=SecretStr("https://secret-pod.example/v1"),
        model="qwen3:8b",
        api_key=SecretStr("route-api-key"),
        secret_headers={"X-Lease-Token": SecretStr("lease-token")},
        context_window=32768,
    )


def make_lease(**overrides) -> InferenceLease:
    values = {
        "lease_id": "lease-123",
        "quote_id": "quote-123",
        "request_id": "request-123",
        "owner_id": "did:kestrel:kite",
        "provider_name": "runpod",
        "state": InferenceLeaseState.READY,
        "model": "qwen3:8b",
        "runtime": "ollama",
        "privacy": InferencePrivacy.AUTHENTICATED_ENDPOINT,
        "created_at": NOW,
        "updated_at": NOW + timedelta(minutes=2),
        "expires_at": NOW + timedelta(minutes=30),
        "region": "us-ks-2",
        "hourly_cost_usd": Decimal("0.57"),
        "estimated_total_cost_usd": Decimal("0.29"),
        "route": make_route(),
        "metadata": {"gpu_class": "rtx-pro-4000"},
    }
    values.update(overrides)
    return InferenceLease(**values)


def test_entry_point_group_is_stable() -> None:
    assert (
        INFERENCE_LEASE_PROVIDER_ENTRY_POINT_GROUP
        == "kestrel_sovereign.inference_lease_providers"
    )


def test_request_is_normalized_deeply_immutable_and_public_owner_free() -> None:
    request = make_request(runtime="OLLAMA", allowed_regions=("US-KS-2",))

    assert request.runtime == "ollama"
    assert request.allowed_regions == ("us-ks-2",)
    assert request.metadata["labels"] == ("private",)
    with pytest.raises(FrozenInstanceError):
        request.model = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        request.metadata["purpose"] = "other"  # type: ignore[index]
    public = request.to_public_dict()
    assert "owner_id" not in public
    assert public["metadata"] == {
        "purpose": "kite-dogfood",
        "labels": ["private"],
    }
    json.dumps(public)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"request_id": None}, "must be a string"),
        ({"request_id": "bad request"}, "request_id"),
        ({"requested_at": NAIVE_NOW}, "timezone-aware"),
        ({"privacy": "authenticated_endpoint"}, "InferencePrivacy"),
        ({"max_hourly_cost_usd": "0.75"}, "must be a Decimal"),
        ({"max_hourly_cost_usd": Decimal("NaN")}, "finite"),
        ({"max_total_cost_usd": Decimal(-1)}, "non-negative"),
        ({"expected_concurrency": True}, "positive integer"),
        ({"idle_ttl_seconds": 1900}, "cannot exceed"),
        ({"capabilities": ("chat", "chat")}, "duplicates"),
        ({"capabilities": "chat"}, "sequence"),
        ({"allowed_regions": ("US", "us")}, "duplicates"),
        ({"allowed_regions": "us-ks-2"}, "sequence"),
    ],
)
def test_request_rejects_malformed_or_unbounded_values(overrides, message) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        make_request(**overrides)


@pytest.mark.parametrize(
    "metadata",
    [
        {"api_key": "plain"},
        {"nested": {"authorization": "bearer plain"}},
        {"access-token": "plain"},
        {"privateKeyPem": "plain"},
    ],
)
def test_public_metadata_rejects_secret_like_keys(metadata) -> None:
    with pytest.raises(ValueError, match="secret-like"):
        make_request(metadata=metadata)


def test_public_metadata_accepts_benign_token_usage_telemetry() -> None:
    request = make_request(
        metadata={
            "total_tokens": 42,
            "prompt_tokens": 12,
            "completion_tokens": 30,
            "tokens_per_second": 25.5,
            "secretary": "not-a-secret-key",
        }
    )

    assert request.to_public_dict()["metadata"]["total_tokens"] == 42


def test_public_metadata_rejects_non_json_and_non_finite_values() -> None:
    with pytest.raises(TypeError, match="JSON"):
        make_request(metadata={"created": NOW})
    with pytest.raises(ValueError, match="non-finite"):
        make_request(metadata={"score": float("inf")})
    with pytest.raises(TypeError, match="mapping"):
        make_request(metadata=None)


def test_capability_matching_covers_privacy_region_shape_and_features() -> None:
    capability = InferenceProviderCapability(
        runtime="ollama",
        privacy=(InferencePrivacy.AUTHENTICATED_ENDPOINT,),
        capabilities=("chat", "tools", "vision"),
        regions=("us-ks-2", "eu-ro-1"),
        max_concurrency=4,
    )

    assert capability.satisfies(make_request())
    assert capability.satisfies(make_request(privacy=InferencePrivacy.PUBLIC_ENDPOINT))
    assert not capability.satisfies(make_request(runtime="vllm"))
    assert not capability.satisfies(
        make_request(privacy=InferencePrivacy.PRIVATE_NETWORK)
    )
    assert not capability.satisfies(make_request(expected_concurrency=5))
    assert not capability.satisfies(make_request(capabilities=("audio",)))
    assert not capability.satisfies(make_request(allowed_regions=("ca-mtl-1",)))


def test_quote_validates_every_pre_provisioning_bound() -> None:
    request = make_request()
    make_quote().validate_for(request, now=NOW)

    cases = [
        (make_quote(request_id="request-other"), "request_id"),
        (make_quote(runtime="vllm"), "runtime"),
        (make_quote(region="eu-ro-1"), "region"),
        (make_quote(hourly_cost_usd=Decimal("0.76")), "hourly"),
        (make_quote(estimated_total_cost_usd=Decimal("0.51")), "total"),
        (make_quote(estimated_ready_seconds=601), "readiness"),
        (make_quote(expires_at=NOW), "expired"),
        (make_quote(privacy=InferencePrivacy.PUBLIC_ENDPOINT), "privacy"),
    ]
    for quote, message in cases:
        with pytest.raises(InferenceLeaseConstraintError, match=message):
            quote.validate_for(request, now=NOW)


def test_quote_public_serialization_is_json_shaped() -> None:
    public = make_quote().to_public_dict()
    assert public["hourly_cost_usd"] == "0.57"
    assert public["privacy"] == "authenticated_endpoint"
    assert public["metadata"] == {"gpu_class": "rtx-pro-4000"}
    json.dumps(public)


def test_quote_allows_zero_second_warm_readiness_estimate() -> None:
    quote = make_quote(estimated_ready_seconds=0)
    quote.validate_for(make_request(), now=NOW)


def test_route_repr_and_public_serialization_never_expose_address_or_secrets() -> None:
    route = make_route()
    rendered = repr(route)
    public_json = json.dumps(route.to_public_dict())

    for secret in (
        "secret-pod.example",
        "route-api-key",
        "lease-token",
        "X-Lease-Token",
    ):
        assert secret not in rendered
        assert secret not in public_json
    assert route.endpoint.get_secret_value() == "https://secret-pod.example/v1"
    assert route.to_public_dict() == {
        "model": "qwen3:8b",
        "protocol": "openai",
        "context_window": 32768,
        "authenticated": True,
    }


def test_route_public_serialization_reports_unauthenticated_private_route() -> None:
    route = InferenceRoute(
        endpoint=SecretStr("http://ollama.internal:11434/v1"),
        model="qwen3:8b",
    )

    assert route.to_public_dict()["authenticated"] is False


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://pod.example/v1",
        "https://user:pass@pod.example/v1",
        "https://pod.example/v1?token=plain",
        "https://pod.example/v1#secret",
        "not-a-url",
    ],
)
def test_route_rejects_unsafe_endpoint_shapes(endpoint) -> None:
    with pytest.raises(ValueError, match="http"):
        InferenceRoute(endpoint=SecretStr(endpoint), model="qwen3:8b")


def test_route_rejects_empty_or_malformed_secret_material() -> None:
    with pytest.raises(TypeError, match="SecretStr"):
        InferenceRoute(
            endpoint="https://pod.example/v1",
            model="qwen3:8b",
        )
    with pytest.raises(TypeError, match="api_key"):
        InferenceRoute(
            endpoint=SecretStr("https://pod.example/v1"),
            model="qwen3:8b",
            api_key="plain",
        )
    with pytest.raises(TypeError, match="header values"):
        InferenceRoute(
            endpoint=SecretStr("https://pod.example/v1"),
            model="qwen3:8b",
            secret_headers={"Authorization": "plain"},
        )
    with pytest.raises(ValueError, match="api_key"):
        InferenceRoute(
            endpoint=SecretStr("https://pod.example/v1"),
            model="qwen3:8b",
            api_key=SecretStr(""),
        )
    with pytest.raises(ValueError, match="header values"):
        InferenceRoute(
            endpoint=SecretStr("https://pod.example/v1"),
            model="qwen3:8b",
            secret_headers={"Authorization": SecretStr("")},
        )


def test_ready_lease_public_serialization_omits_owner_endpoint_and_credentials() -> (
    None
):
    lease = make_lease()
    public_json = json.dumps(lease.to_public_dict())

    assert lease.is_terminal is False
    assert lease.to_public_dict()["route"]["authenticated"] is True
    assert "did:kestrel:kite" not in repr(lease)
    for secret in (
        "did:kestrel:kite",
        "secret-pod.example",
        "route-api-key",
        "lease-token",
    ):
        assert secret not in public_json


@pytest.mark.parametrize(
    "state",
    [
        InferenceLeaseState.PENDING,
        InferenceLeaseState.RELEASING,
        InferenceLeaseState.RELEASED,
        InferenceLeaseState.EXPIRED,
        InferenceLeaseState.FAILED,
    ],
)
def test_non_ready_lease_rejects_route(state) -> None:
    overrides = {"state": state}
    if state is InferenceLeaseState.FAILED:
        overrides["failure"] = InferenceLeaseFailure(
            code="provider_failed",
            message="Provider failed safely",
        )
    with pytest.raises(ValueError, match="only a ready"):
        make_lease(**overrides)


def test_ready_requires_route_and_failed_requires_failure() -> None:
    with pytest.raises(ValueError, match="ready lease requires"):
        make_lease(route=None)
    with pytest.raises(ValueError, match="failed lease requires"):
        make_lease(state=InferenceLeaseState.FAILED, route=None)
    with pytest.raises(ValueError, match="only a failed"):
        make_lease(
            failure=InferenceLeaseFailure(code="bad", message="not allowed"),
        )
    with pytest.raises(ValueError, match="route model"):
        make_lease(
            route=InferenceRoute(
                endpoint=SecretStr("https://pod.example/v1"),
                model="other:8b",
            )
        )
    with pytest.raises(ValueError, match="already be expired"):
        make_lease(expires_at=NOW + timedelta(minutes=1))
    with pytest.raises(ValueError, match="reached its expiry"):
        make_lease(
            state=InferenceLeaseState.EXPIRED,
            route=None,
            expires_at=NOW + timedelta(minutes=30),
        )


def test_terminal_state_and_owner_isolation() -> None:
    lease = make_lease(state=InferenceLeaseState.RELEASED, route=None)

    assert lease.is_terminal is True
    lease.assert_owner("did:kestrel:kite")
    with pytest.raises(InferenceLeaseOwnershipError):
        lease.assert_owner("did:kestrel:nellie")


def test_failed_lease_public_serialization_is_sanitized_and_json_shaped() -> None:
    lease = make_lease(
        state=InferenceLeaseState.FAILED,
        route=None,
        failure=InferenceLeaseFailure(
            code="capacity_unavailable",
            message="No matching capacity is currently available",
            retryable=True,
            metadata={"retry_after_seconds": 30, "total_tokens": 0},
        ),
    )

    public = lease.to_public_dict()
    assert public["failure"] == {
        "code": "capacity_unavailable",
        "message": "No matching capacity is currently available",
        "retryable": True,
        "metadata": {"retry_after_seconds": 30, "total_tokens": 0},
    }
    rendered = json.dumps(public)
    assert "did:kestrel:kite" not in rendered
    assert "secret-pod.example" not in rendered


def test_failure_quote_and_lease_metadata_all_reject_secret_keys() -> None:
    with pytest.raises(ValueError, match="secret-like"):
        InferenceLeaseFailure(
            code="failed",
            message="Sanitized failure",
            metadata={"access_token": "plain"},
        )
    with pytest.raises(ValueError, match="secret-like"):
        make_quote(metadata={"apiKey": "plain"})
    with pytest.raises(ValueError, match="secret-like"):
        make_lease(metadata={"private_key": "plain"})


def test_realized_lease_validates_against_request_and_quote() -> None:
    request = make_request()
    quote = make_quote()
    make_lease().validate_for(request, quote)

    mismatches = [
        (make_lease(owner_id="did:kestrel:other"), "owner"),
        (make_lease(quote_id="quote-other"), "quote_id"),
        (make_lease(provider_name="vastai"), "provider"),
        (
            make_lease(
                model="other:8b",
                route=InferenceRoute(
                    endpoint=SecretStr("https://pod.example/v1"),
                    model="other:8b",
                    api_key=SecretStr("key"),
                ),
            ),
            "model",
        ),
        (make_lease(privacy=InferencePrivacy.PUBLIC_ENDPOINT), "privacy"),
        (make_lease(region="eu-ro-1"), "region"),
        (make_lease(hourly_cost_usd=Decimal("0.58")), "hourly"),
        (make_lease(estimated_total_cost_usd=Decimal("0.30")), "total"),
    ]
    for lease, message in mismatches:
        with pytest.raises(InferenceLeaseConstraintError, match=message):
            lease.validate_for(request, quote)


def test_realized_lease_rejects_expiry_beyond_request_session_deadline() -> None:
    request = make_request()
    quote = make_quote()
    malicious_lease = make_lease(
        expires_at=request.requested_at
        + timedelta(
            seconds=(
                request.ready_deadline_seconds + request.expected_session_seconds + 1
            )
        )
    )

    with pytest.raises(InferenceLeaseConstraintError, match="session deadline"):
        malicious_lease.validate_for(request, quote)


def test_realized_lease_rejects_touch_that_extends_past_session_deadline() -> None:
    """A renewal actually returned by ``touch`` is held to the same ceiling.

    Driven through a provider rather than a hand-built lease so this covers
    the renewal path the v6 contract added, instead of restating the
    constraint check already asserted above.
    """
    request = make_request()
    quote = make_quote()
    latest_expiry = request.requested_at + timedelta(
        seconds=request.ready_deadline_seconds + request.expected_session_seconds
    )
    make_lease(expires_at=latest_expiry).validate_for(request, quote)

    class OvereagerRenewalProvider(CompleteProvider):
        """Renews one second past the window the request authorized."""

        async def touch(self, owner_id, lease_id):
            return make_lease(
                owner_id=owner_id,
                lease_id=lease_id,
                updated_at=latest_expiry - timedelta(minutes=1),
                expires_at=latest_expiry + timedelta(seconds=1),
            )

    async def exercise() -> None:
        provider = OvereagerRenewalProvider()
        renewed_lease = await provider.touch(request.owner_id, "lease-renewal")
        with pytest.raises(
            InferenceLeaseConstraintError, match="session deadline"
        ):
            renewed_lease.validate_for(request, quote)

    asyncio.run(exercise())


def test_lease_expiry_check_stays_in_taxonomy_for_extreme_session_bounds() -> None:
    """The absolute-lifetime check must fail closed, never ``OverflowError``.

    ``expected_session_seconds`` and ``ready_deadline_seconds`` are bounded
    only from below, so the SDK accepts a large-but-well-formed request. The
    ceiling must therefore be computed without materializing a ``timedelta``
    that cannot exist: ``OverflowError`` is an ``ArithmeticError`` and would
    escape the ``InferenceLeaseError``/``ValueError`` taxonomy this module
    documents, defeating every caller that guards ``validate_for``.
    """
    quote = make_quote()
    for session_seconds in (10**12, 10**14):
        request = make_request(
            expected_session_seconds=session_seconds, idle_ttl_seconds=60
        )
        # The lease sits far inside so vast a window, so the ceiling is simply
        # not reached — the point is that evaluating it does not explode.
        make_lease().validate_for(request, quote)


class CompleteProvider:
    provider_name = "example"

    def capabilities(self):
        return ()

    def is_available(self):
        return True

    async def quote(self, request):
        return make_quote(request_id=request.request_id)

    async def acquire(self, request, quote):
        return make_lease(request_id=request.request_id, owner_id=request.owner_id)

    async def status(self, owner_id, lease_id):
        return make_lease(owner_id=owner_id, lease_id=lease_id)

    async def touch(self, owner_id, lease_id):
        return make_lease(
            owner_id=owner_id,
            lease_id=lease_id,
            updated_at=NOW + timedelta(minutes=3),
            expires_at=NOW + timedelta(minutes=33),
        )

    async def release(self, owner_id, lease_id):
        return make_lease(
            owner_id=owner_id,
            lease_id=lease_id,
            state=InferenceLeaseState.RELEASED,
            route=None,
        )


class IncompleteProvider:
    provider_name = "incomplete"


# Derived from ``CompleteProvider`` so the two can never drift apart: this
# class differs from a conforming provider by exactly one member, ``touch``.
ProviderMissingTouch = type(
    "ProviderMissingTouch",
    (),
    {
        name: value
        for name, value in vars(CompleteProvider).items()
        if name not in {"touch", "__dict__", "__weakref__"}
    },
)


def test_provider_protocol_is_runtime_checkable() -> None:
    assert isinstance(CompleteProvider(), InferenceLeaseProvider)
    assert not isinstance(IncompleteProvider(), InferenceLeaseProvider)


def test_provider_missing_only_touch_fails_contract_v6() -> None:
    """``touch`` must be load-bearing, not merely documented.

    ``IncompleteProvider`` fails the protocol for many reasons at once, so it
    cannot show that contract v6 actually requires ``touch``.  This provider
    implements every other member, so the isinstance check can only fail on
    the operation v6 added — which is what makes "required" enforceable at
    entry-point load time rather than at first real-traffic call.
    """
    assert not hasattr(ProviderMissingTouch, "touch")
    unexpectedly_missing = tuple(
        name
        for name in (
            "provider_name",
            "capabilities",
            "is_available",
            "quote",
            "acquire",
            "status",
            "release",
        )
        if not hasattr(ProviderMissingTouch, name)
    )
    assert not unexpectedly_missing, (
        f"fixture lost unrelated members: {unexpectedly_missing}"
    )

    assert not isinstance(ProviderMissingTouch(), InferenceLeaseProvider)


def test_complete_provider_async_contract_is_awaitable() -> None:
    async def exercise() -> None:
        provider = CompleteProvider()
        request = make_request()
        quote = await provider.quote(request)
        quote.validate_for(request, now=NOW)
        lease = await provider.acquire(request, quote)
        lease.validate_for(request, quote)
        status = await provider.status(request.owner_id, lease.lease_id)
        assert status.state is InferenceLeaseState.READY
        touched = await provider.touch(request.owner_id, lease.lease_id)
        assert touched.state is InferenceLeaseState.READY
        assert touched.expires_at > status.expires_at
        touched.validate_for(request, quote)
        released = await provider.release(request.owner_id, lease.lease_id)
        assert released.state is InferenceLeaseState.RELEASED

    asyncio.run(exercise())
