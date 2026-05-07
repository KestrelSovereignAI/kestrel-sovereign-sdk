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


def test_to_dict_exact_shape_for_post_response_event():
    """Pin the full wire shape of a representative POST_RESPONSE
    payload — accidental key renames, omissions, or reorderings would
    break Claude-Code-style stdin/stdout JSON hook consumers that do
    exact-key dispatch."""
    h = HookInput(
        session_id="sess-1",
        hook_event_name=HookEvent.POST_RESPONSE.value,
        response_text="Found 3 results.",
        pre_tool_prose="Looking that up now...",
        tool_calls=[{"id": "tc1", "name": "search", "arguments": {"q": "x"}}],
        tool_results=[{"status": "ok", "data": {"count": 3}}],
    )
    d = h.to_dict()
    # Drop timestamp (volatile) and assert the remaining keys/values.
    d.pop("timestamp")
    assert d == {
        "session_id": "sess-1",
        "hook_event_name": "PostResponse",
        "cwd": "",
        "tool_name": None,
        "tool_input": None,
        "feature_name": None,
        "tool_response": None,
        "execution_time_ms": None,
        "user_message": None,
        "response_text": "Found 3 results.",
        "pre_tool_prose": "Looking that up now...",
        "tool_calls": [{"id": "tc1", "name": "search", "arguments": {"q": "x"}}],
        "tool_results": [{"status": "ok", "data": {"count": 3}}],
        "parent_did": None,
        "child_did": None,
        "child_name": None,
        "spawn_purpose": None,
        "termination_reason": None,
    }


def test_positional_args_through_agent_spawn_keep_pre_0_9_meaning():
    """Pin the field-order compatibility codex flagged at review.

    Pre-0.9 callers that constructed HookInput positionally up through
    the AgentSpawn fields must keep those positional values aligned to
    ``parent_did`` / ``child_did`` / etc. — NOT silently re-routed onto
    the new ``pre_tool_prose`` / ``tool_calls`` / ``tool_results``
    fields by 0.9's additions. Exercises every positional slot from
    ``session_id`` through ``termination_reason`` so a future field
    insertion in the middle would break this guard immediately.
    """
    from datetime import datetime
    ts = datetime(2026, 5, 7, 22, 0, 0)
    h = HookInput(
        "sess-1",                          # 1: session_id
        HookEvent.POST_RESPONSE.value,     # 2: hook_event_name
        "/cwd",                            # 3: cwd
        ts,                                # 4: timestamp
        "tool-x",                          # 5: tool_name
        {"k": "v"},                        # 6: tool_input
        "FeatX",                           # 7: feature_name
        {"status": "ok"},                  # 8: tool_response
        42,                                # 9: execution_time_ms
        "hi",                              # 10: user_message
        "rt",                              # 11: response_text
        "parent-did",                      # 12: parent_did
        "child-did",                       # 13: child_did
        "Junior",                          # 14: child_name
        "spawn-because",                   # 15: spawn_purpose
        "term-because",                    # 16: termination_reason
    )
    # Existing fields receive their positional values.
    assert h.session_id == "sess-1"
    assert h.parent_did == "parent-did"
    assert h.child_did == "child-did"
    assert h.child_name == "Junior"
    assert h.spawn_purpose == "spawn-because"
    assert h.termination_reason == "term-because"
    # New 0.9 fields default to None — NOT overrun by any of the
    # positional values above.
    assert h.pre_tool_prose is None
    assert h.tool_calls is None
    assert h.tool_results is None


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
