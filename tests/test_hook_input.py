"""SDK 0.9 — HookInput narration-check fields (kestrel-sovereign #1048 Wave 5D).

ResponseAuditHook needs structured access to:
* the pre-tool prose the agent streamed before any tool fired,
* the tool calls that were issued,
* the tool results that came back,

so it can write a deterministic narration check (was the agent's
pre-tool past-tense success language consistent with the actual tool
results?). Until 0.9 the hook only saw ``response_text`` (the
post-tool synthesis).

Pinning tests guard the contract:
* Field presence and defaults.
* ``to_dict()`` round-trip (the wire shape matters for Claude-Code-style
  stdin/stdout JSON hooks; missing keys would break consumers).
* Backward compatibility — pre-0.9 callers that only set
  ``response_text`` still construct cleanly.
"""
from kestrel_sdk.hooks.base import HookEvent, HookInput


def test_post_response_narration_fields_present_and_default_to_none():
    h = HookInput(session_id="s", hook_event_name=HookEvent.POST_RESPONSE.value)
    assert h.pre_tool_prose is None
    assert h.tool_calls is None
    assert h.tool_results is None


def test_post_response_narration_fields_round_trip_through_to_dict():
    h = HookInput(
        session_id="s",
        hook_event_name=HookEvent.POST_RESPONSE.value,
        response_text="Found 3 results.",
        pre_tool_prose="Looking that up now...",
        tool_calls=[{"id": "tc1", "name": "search", "arguments": {"q": "x"}}],
        tool_results=[{"status": "ok", "data": {"count": 3}}],
    )
    d = h.to_dict()
    assert d["pre_tool_prose"] == "Looking that up now..."
    assert d["tool_calls"] == [
        {"id": "tc1", "name": "search", "arguments": {"q": "x"}}
    ]
    assert d["tool_results"] == [{"status": "ok", "data": {"count": 3}}]
    assert d["response_text"] == "Found 3 results."


def test_pre_0_9_callers_still_construct_without_narration_fields():
    """Backwards compat: existing POST_RESPONSE consumers that only
    set ``response_text`` keep working — the new fields default to
    ``None`` and don't change the wire shape's required keys."""
    h = HookInput(
        session_id="s",
        hook_event_name=HookEvent.POST_RESPONSE.value,
        response_text="Hello.",
    )
    assert h.response_text == "Hello."
    # to_dict() now includes the new keys, but as None — consumers that
    # ignore unknown keys (or null values) continue to work.
    d = h.to_dict()
    assert d["pre_tool_prose"] is None
    assert d["tool_calls"] is None
    assert d["tool_results"] is None
