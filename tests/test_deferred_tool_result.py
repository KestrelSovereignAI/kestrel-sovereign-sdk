"""Private delivery cannot escape via serialization, copying or redaction."""

from copy import copy, deepcopy
from functools import partial
import json
import pickle
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from kestrel_sdk.tools import DeferredToolResult, DeferredToolResultHost, FinalizedToolResult, ToolResult
from kestrel_sdk.features.base import Feature, tool
from kestrel_sdk.tools import ToolCategory, current_tool_result_parts


def test_finalization_returns_plain_envelope_and_only_new_parts_once():
    calls = []
    original = ToolResult.ok("Complete", data={"value": [1]},
        parts=[{"type": "original", "data": "first"}]).to_dict()
    extra = [{"type": "private_view", "data": {"reference": "opaque"}}]

    def publish():
        calls.append("published")
        return extra

    result = DeferredToolResult(original, publish=publish)
    original["data"]["value"].append(2)
    delivery = result.finalize(result, allowed=True)
    assert isinstance(delivery, FinalizedToolResult)
    envelope, parts = delivery.result, delivery.parts
    assert delivery.withheld is False
    assert type(envelope) is dict
    assert envelope["data"] == {"value": [1]}
    assert envelope["parts"] == [{"type": "original", "data": "first"}, *extra]
    assert parts == extra and calls == ["published"]
    extra[0]["data"]["reference"] = "changed"
    assert parts[0]["data"]["reference"] == "opaque"
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True)
    assert calls == ["published"]


def test_post_tool_redaction_withholds_and_consumes_private_delivery():
    def forbidden():
        pytest.fail("Post-tool redaction was bypassed by private render delivery")

    result = DeferredToolResult(ToolResult.ok("Complete", data={"secret": "original"}).to_dict(),
        publish=forbidden)
    result["data"]["secret"] = "redacted"
    delivery = result.finalize(result, allowed=True)
    envelope, parts = delivery.result, delivery.parts
    assert delivery.withheld is True
    assert envelope["data"] == {"secret": "redacted"} and not parts
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True)


def test_copies_and_json_carry_no_private_delivery_capability():
    private = {"native_meta": "must-never-serialize"}
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(),
        publish=lambda: [{"type": "view", "data": private}])
    for projection in [copy(result), deepcopy(result), json.loads(json.dumps(result))]:
        assert type(projection) is dict
        assert projection == ToolResult.ok("Complete").to_dict()
        assert "must-never-serialize" not in repr(projection)
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(result)
    assert result.finalize(result, allowed=True).parts == [{"type": "view", "data": private}]


@pytest.mark.parametrize("parts", [None, "invalid", [{"type": ""}], ["not-a-part"]])
def test_invalid_delivery_uses_normal_parts_validation_and_cannot_retry(parts):
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=lambda: parts)
    with pytest.raises((TypeError, ValueError)):
        result.finalize(result, allowed=True)
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True)


def test_failed_factory_is_consumed_without_an_implicit_retry():
    calls = []

    def fail():
        calls.append("once")
        raise ValueError("failed delivery")

    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=fail)
    with pytest.raises(ValueError, match="failed delivery"):
        result.finalize(result, allowed=True)
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True)
    assert calls == ["once"]


def test_constructor_rejects_invalid_contracts_without_running_the_factory():
    async def asynchronous():
        return []

    for factory in [None, asynchronous]:
        with pytest.raises(TypeError, match="synchronous callable"):
            DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=factory)
    with pytest.raises(TypeError, match="dictionary"):
        DeferredToolResult([], publish=lambda: [])


@pytest.mark.asyncio
async def test_actual_sdk_decorator_preserves_deferred_delivery_and_part_order():
    class Source(Feature):
        tool_description = "Owned deferred wrapper fixture"

        async def initialize(self):
            pass

        @tool(name="private_view", description="Owned deferred output", category=ToolCategory.UTILITY)
        async def private_view(self):
            current_tool_result_parts().append({"type": "buffered", "data": {}})
            self.original = DeferredToolResult(ToolResult.ok("Complete",
                parts=[{"type": "explicit", "data": {}}]).to_dict(),
                publish=lambda: [{"type": "deferred", "data": {"ref": "opaque"}}])
            return self.original

    feature = Source(None)
    wrapped = next(item for item in feature.get_tools() if item.name == "private_view")
    result = await wrapped.execute()
    assert isinstance(result, DeferredToolResult), "The SDK wrapped private delivery as a legacy nested result"
    assert result["status"] == "ok" and result["tool"] == "private_view"
    assert "success" not in result
    assert [item["type"] for item in result["parts"]] == ["buffered", "explicit"]
    delivery = result.finalize(result, allowed=True)
    envelope, parts = delivery.result, delivery.parts
    assert [item["type"] for item in envelope["parts"]] == ["buffered", "explicit", "deferred"]
    with pytest.raises(RuntimeError, match="already finalized"):
        feature.original.finalize(feature.original, allowed=True)


def test_delivered_parts_cannot_mutate_the_envelope_copy():
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(),
        publish=lambda: [{"type": "view", "data": {"reference": "original"}}])
    delivery = result.finalize(result, allowed=True)
    envelope, parts = delivery.result, delivery.parts
    assert envelope["parts"][0] is not parts[0]
    parts[0]["data"]["reference"] = "changed"
    assert envelope["parts"][0]["data"]["reference"] == "original"


@pytest.mark.parametrize("invalid", [{}, {"status": "ok", "confirmation": "Done", "parts": "invalid"}])
def test_invalid_original_envelope_fails_at_construction(invalid):
    with pytest.raises((TypeError, ValueError)):
        DeferredToolResult(invalid, publish=lambda: [])


def test_parts_are_validated_after_their_copy():
    class ChangesWhenCopied(dict):
        def __deepcopy__(self, memo):
            return {"type": "", "data": {}}

    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(),
        publish=lambda: [ChangesWhenCopied(type="apparently-valid")])
    with pytest.raises(ValueError, match="non-empty string"):
        result.finalize(result, allowed=True)


@pytest.mark.parametrize("redact", [False, True])
def test_a_replaced_post_hook_object_withholds_even_if_its_values_are_unchanged(redact):
    calls = []
    result = DeferredToolResult(ToolResult.ok("Complete", data={"value": "original"}).to_dict(),
        publish=lambda: calls.append("must-not-run") or [])
    replacement = deepcopy(result)
    if redact:
        replacement["data"]["value"] = "redacted"
    delivery = result.finalize(replacement, allowed=True)
    assert delivery.withheld is True and delivery.parts == []
    assert delivery.result == replacement and not calls
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True)


def test_an_empty_delivery_is_distinct_from_policy_withholding():
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=lambda: [])
    delivery = result.finalize(result, allowed=True)
    assert delivery.withheld is False and delivery.parts == []


def test_wrapper_cannot_rebaseline_an_already_changed_public_result():
    calls = []
    result = DeferredToolResult(ToolResult.ok("Complete", data={"value": "original"}).to_dict(),
        publish=lambda: calls.append("must-not-run") or [])
    result["data"]["value"] = "redacted"
    with pytest.raises(ValueError, match="changed before wrapper"):
        result.for_tool("owned", [])
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True)
    assert not calls


def test_shared_delivery_has_only_one_successful_consumer_across_threads():
    calls = []
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=lambda: calls.append("once") or [])
    barrier = Barrier(8)

    def consume():
        barrier.wait(timeout=5)
        try:
            return result.finalize(result, allowed=True).withheld
        except RuntimeError as error:
            assert "already finalized" in str(error)
            return "consumed"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: consume(), range(8)))
    assert outcomes.count(False) == 1 and outcomes.count("consumed") == 7
    assert calls == ["once"]


@pytest.mark.parametrize("original, success", [(ToolResult.ok("Complete"), True),
    (ToolResult.failed("Refused"), False), (ToolResult.partial("Complete", "UI unavailable"), True)])
def test_core_wrapper_can_add_its_canonical_success_projection(original, success):
    result = DeferredToolResult(original.to_dict(), publish=lambda: [])
    wrapped = result.for_tool("owned", [], include_success=True)
    assert wrapped["success"] is success
    assert wrapped.finalize(wrapped, allowed=True).withheld is False


def test_decision_only_withholding_consumes_unchanged_delivery_without_calling_the_factory():
    calls = []
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(),
        publish=lambda: calls.append("must-not-publish") or [])
    delivery = result.finalize(result, allowed=False)
    assert delivery.withheld and delivery.parts == []
    assert delivery.result == dict(result) and calls == []
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True)


@pytest.mark.parametrize("allowed", [None, 1, "yes", []])
def test_invalid_policy_decisions_fail_and_consume_without_publication(allowed):
    calls = []
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(),
        publish=lambda: calls.append("must-not-publish") or [])
    with pytest.raises(TypeError, match="explicit boolean"):
        result.finalize(result, allowed=allowed)
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True)
    assert not calls


@pytest.mark.parametrize("arguments", [
    {"name": ""}, {"name": None}, {"pending_parts": None},
    {"pending_parts": "invalid"}, {"pending_parts": [{"type": ""}]},
    {"include_success": "yes"},
])
def test_bad_wrapper_metadata_consumes_the_original_factory(arguments):
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=lambda: [])
    options = {"name": "owned", "pending_parts": [], **arguments}
    with pytest.raises((TypeError, ValueError)):
        result.for_tool(**options)
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True)


def test_partial_async_callable_is_refused_without_creating_a_coroutine():
    class AsyncFactory:
        async def __call__(self):
            return []

    with pytest.raises(TypeError, match="synchronous callable"):
        DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=partial(AsyncFactory()))


def test_direct_serialization_hooks_cannot_return_a_private_factory():
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=lambda: [])
    for operation in (result.__reduce__, result.__getstate__):
        with pytest.raises(TypeError, match="cannot be pickled"):
            operation()


def test_host_protocol_names_the_actual_agent_capability():
    class Agent:
        supports_deferred_tool_results = True

    assert isinstance(Agent(), DeferredToolResultHost)
    assert Agent().supports_deferred_tool_results is True


@pytest.mark.parametrize("flag", [False, 1])
def test_structural_host_introspection_cannot_replace_the_positive_capability_gate(flag):
    class Agent:
        supports_deferred_tool_results = flag

    agent = Agent()
    assert isinstance(agent, DeferredToolResultHost)
    assert (getattr(agent, "supports_deferred_tool_results", False) is True) is False


def test_non_dictionary_policy_result_consumes_delivery_without_publication():
    calls = []
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(),
        publish=lambda: calls.append("must-not-run") or [])
    with pytest.raises(TypeError, match="dictionary envelope"):
        result.finalize([], allowed=True)
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True)
    assert not calls


def test_missing_policy_argument_fails_before_entering_the_delivery_method():
    calls = []
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(),
        publish=lambda: calls.append("must-not-run") or [])
    with pytest.raises(TypeError, match="allowed"):
        result.finalize(result)
    assert result.finalize(result, allowed=False).withheld
    assert not calls
