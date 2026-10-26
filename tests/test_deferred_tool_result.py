"""Private delivery cannot escape via serialization, copying or redaction."""

from copy import copy, deepcopy
import json
import pickle

import pytest

from kestrel_sdk.tools import DeferredToolResult, ToolResult


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
    envelope, parts = result.finalize()
    assert type(envelope) is dict
    assert envelope["data"] == {"value": [1]}
    assert envelope["parts"] == [{"type": "original", "data": "first"}, *extra]
    assert parts == extra and calls == ["published"]
    extra[0]["data"]["reference"] = "changed"
    assert parts[0]["data"]["reference"] == "opaque"
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize()
    assert calls == ["published"]


def test_post_tool_redaction_withholds_and_consumes_private_delivery():
    def forbidden():
        pytest.fail("Post-tool redaction was bypassed by private render delivery")

    result = DeferredToolResult(ToolResult.ok("Complete", data={"secret": "original"}).to_dict(),
        publish=forbidden)
    result["data"]["secret"] = "redacted"
    envelope, parts = result.finalize()
    assert envelope["data"] == {"secret": "redacted"} and not parts
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize()


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
    assert result.finalize()[1] == [{"type": "view", "data": private}]


@pytest.mark.parametrize("parts", [None, "invalid", [{"type": ""}], ["not-a-part"]])
def test_invalid_delivery_uses_normal_parts_validation_and_cannot_retry(parts):
    result = DeferredToolResult({}, publish=lambda: parts)
    with pytest.raises((TypeError, ValueError)):
        result.finalize()
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize()


def test_failed_factory_is_consumed_without_an_implicit_retry():
    calls = []

    def fail():
        calls.append("once")
        raise ValueError("failed delivery")

    result = DeferredToolResult({}, publish=fail)
    with pytest.raises(ValueError, match="failed delivery"):
        result.finalize()
    with pytest.raises(RuntimeError, match="already finalized"):
        result.finalize()
    assert calls == ["once"]


def test_constructor_rejects_invalid_contracts_without_running_the_factory():
    async def asynchronous():
        return []

    for factory in [None, asynchronous]:
        with pytest.raises(TypeError, match="synchronous callable"):
            DeferredToolResult({}, publish=factory)
    with pytest.raises(TypeError, match="dictionary"):
        DeferredToolResult([], publish=lambda: [])
