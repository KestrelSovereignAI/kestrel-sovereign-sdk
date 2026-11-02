"""Private delivery cannot escape via serialization, copying or redaction."""

from copy import copy, deepcopy
from functools import partial
import json
import pickle
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from threading import Event
import signal
import sys
import inspect

import pytest

from kestrel_sdk.tools import (
    DeferredToolResult, DeferredToolResultHost, FinalizedToolResult, ToolResult,
    ToolPublicationContext, ToolPublicationHost,
)
from kestrel_sdk.features.base import Feature, tool
from kestrel_sdk.tools import ToolCategory, current_tool_result_parts


def test_publication_context_exists_only_during_the_original_transferred_factory():
    context = ToolPublicationContext("did:test:publication", "owned-chat")
    observed = []
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(),
        publish=lambda: observed.append(result.publication_context) or [])
    first = result.for_tool("first_wrapper", [])
    transferred = first.for_tool("owned_tool", [])
    assert result.publication_context is None and transferred.publication_context is None
    assert first.publication_context is None
    delivery = transferred.finalize(transferred, allowed=True, context=context)
    assert observed == [context] and delivery.withheld is False
    assert result.publication_context is None and transferred.publication_context is None
    assert "owned-chat" not in json.dumps(delivery.result)
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True, context=context)


@pytest.mark.parametrize("failure", ["deny", "redact", "replace", "factory-error", "invalid-parts"])
def test_failed_or_withheld_publication_never_retains_a_host_destination(failure):
    context = ToolPublicationContext("did:test:publication", "owned-chat")
    observed = []

    def publish():
        observed.append(result.publication_context)
        if failure == "factory-error":
            raise RuntimeError("Owned publication failed")
        return None if failure == "invalid-parts" else []

    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=publish)
    projected = result
    if failure == "redact":
        result["confirmation"] = "Reviewed redaction"
    if failure == "replace":
        projected = ToolResult.ok("Reviewed replacement").to_dict()
    if failure in {"factory-error", "invalid-parts"}:
        with pytest.raises((RuntimeError, TypeError)):
            result.finalize(projected, allowed=True, context=context)
    else:
        assert result.finalize(projected, allowed=failure != "deny", context=context).withheld
    assert result.publication_context is None
    assert observed == ([context] if failure in {"factory-error", "invalid-parts"} else [])
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True, context=context)


def test_nested_publications_have_independent_destinations_and_headless_is_explicit():
    outer_context = ToolPublicationContext("did:test:outer", "outer-chat")
    inner_context = ToolPublicationContext("did:test:inner", "inner-chat")
    observed = []
    inner = DeferredToolResult(ToolResult.ok("Inner").to_dict(),
        publish=lambda: observed.append(("inner", inner.publication_context)) or [])
    headless = DeferredToolResult(ToolResult.ok("Headless").to_dict(),
        publish=lambda: observed.append(("headless", headless.publication_context)) or [])

    def publish_outer():
        observed.append(("outer", outer.publication_context))
        inner.finalize(inner, allowed=True, context=inner_context)
        headless.finalize(headless, allowed=True)
        observed.append(("outer-after", outer.publication_context))
        assert inner.publication_context is None and headless.publication_context is None
        return []

    outer = DeferredToolResult(ToolResult.ok("Outer").to_dict(), publish=publish_outer)
    outer.finalize(outer, allowed=True, context=outer_context)
    assert observed == [("outer", outer_context), ("inner", inner_context),
                        ("headless", None), ("outer-after", outer_context)]
    assert outer.publication_context is None


def test_concurrent_results_cannot_borrow_each_others_publication_destination():
    barrier = Barrier(2, timeout=5)
    contexts = [ToolPublicationContext(f"did:test:agent-{i}", f"owned-chat-{i}") for i in range(2)]
    results = []
    observed = [None, None]
    for index in range(2):
        def publish(index=index):
            barrier.wait()
            observed[index] = results[index].publication_context
            barrier.wait()
            return []
        results.append(DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=publish))
    with ThreadPoolExecutor(max_workers=2) as executor:
        completed = [executor.submit(result.finalize, result, allowed=True, context=context)
                     for result, context in zip(results, contexts)]
        for future in completed:
            assert future.result(timeout=10).withheld is False
    assert observed == contexts
    assert all(result.publication_context is None for result in results)


def test_invalid_publication_context_is_rejected_and_consumes_the_factory():
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(),
        publish=lambda: pytest.fail("Untrusted destination reached publication"))
    with pytest.raises(TypeError, match="ToolPublicationContext"):
        result.finalize(result, allowed=True, context={"agent_id": "forged", "session_id": "forged"})
    assert result.publication_context is None
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True)


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_publication_context_is_cleared_on_base_exception(error):
    context = ToolPublicationContext("did:test:publication", "owned-chat")
    original = error("Owned interrupted publication")

    def publish():
        assert result.publication_context is context
        raise original

    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=publish)
    with pytest.raises(error) as captured:
        result.finalize(result, allowed=True, context=context)
    assert captured.value is original and result.publication_context is None


def test_a_physical_interrupt_after_context_assignment_cannot_retain_the_destination():
    context = ToolPublicationContext("did:test:publication", "owned-chat")
    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=lambda: [])
    previous_trace = sys.gettrace()
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    lines, first_line = inspect.getsourcelines(DeferredToolResult.finalize)
    assignment_line = next(first_line + i for i, line in enumerate(lines)
                           if line.strip() == "self._delivery_state.context = context")

    def interrupt_after_assignment(frame, event, arg):
        if (frame.f_code is DeferredToolResult.finalize.__code__ and event == "line"
                and frame.f_lineno > assignment_line
                and result._delivery_state.context is context):
            sys.settrace(None)
            signal.raise_signal(signal.SIGINT)
        return interrupt_after_assignment

    try:
        sys.settrace(interrupt_after_assignment)
        with pytest.raises(KeyboardInterrupt):
            result.finalize(result, allowed=True, context=context)
    finally:
        sys.settrace(previous_trace)
        signal.signal(signal.SIGINT, previous_handler)
    assert result.publication_context is None


def test_publication_cleanup_does_not_wait_for_a_suspended_metadata_reader():
    context = ToolPublicationContext("did:test:publication", "owned-chat")
    started, release = Event(), Event()

    def publish():
        started.set()
        assert release.wait(5)
        return []

    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=publish)
    waited_for_reader = False
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(result.finalize, result, allowed=True, context=context)
        assert started.wait(5)
        # A reader may be descheduled while holding its real lock. Completing
        # publication must clear metadata without that reader's cooperation.
        with result._delivery_state.lock:
            release.set()
            try:
                future.result(timeout=0.2)
            except TimeoutError:
                waited_for_reader = True
        assert future.result(timeout=5).withheld is False
    assert not waited_for_reader
    assert result.publication_context is None


def test_subclass_cannot_replace_the_frozen_host_destination_contract():
    class MutableContext(ToolPublicationContext):
        pass

    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=lambda: [])
    with pytest.raises(TypeError, match="ToolPublicationContext"):
        result.finalize(result, allowed=True, context=MutableContext("agent", "chat"))
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize(result, allowed=True)


def test_publication_context_does_not_extend_into_part_copying():
    context = ToolPublicationContext("did:test:publication", "owned-chat")
    observed = []

    class Part(dict):
        def __deepcopy__(self, memo):
            observed.append(result.publication_context)
            return dict(self)

    def publish():
        assert result.publication_context is context
        assert "owned-chat" not in json.dumps(result)
        return [Part(type="owned_view", data={"id": "opaque-reference"})]

    result = DeferredToolResult(ToolResult.ok("Complete").to_dict(), publish=publish)
    delivery = result.finalize(result, allowed=True, context=context)
    assert observed == [None] and delivery.parts[0]["type"] == "owned_view"
    assert result.publication_context is None


@pytest.mark.parametrize("agent_id,session_id", [("", "chat"), ("agent", " "), (None, "chat"), ("agent", 1)])
def test_publication_destination_requires_actual_host_identifiers(agent_id, session_id):
    with pytest.raises(ValueError, match="non-empty string"):
        ToolPublicationContext(agent_id, session_id)


@pytest.mark.parametrize("flag", [True, False, 1, None])
def test_publication_host_is_structural_and_consumers_must_check_the_exact_flag(flag):
    class Host:
        supports_deferred_tool_results = True
        supports_tool_publication_context = flag

    assert isinstance(Host(), ToolPublicationHost)
    assert not isinstance(object(), ToolPublicationHost)
    assert (getattr(Host(), "supports_tool_publication_context", False) is True) is (flag is True)


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
