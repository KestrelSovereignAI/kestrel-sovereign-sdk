"""Tests for ToolResult — the cross-feature tool envelope contract.

The honesty layer (issue #1042) reads ``status`` deterministically;
these tests pin every shape rule the registry validator and audit
hook will rely on. Any change here is a contract change — read the
PR body before touching them.
"""

from __future__ import annotations

import pytest

from kestrel_sdk.tools.result import ToolResult, ToolResultStatus


# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------


class TestToolResultStatus:
    def test_values_are_canonical_lowercase(self):
        """Wire format is the lowercase string for every status."""
        assert ToolResultStatus.OK.value == "ok"
        assert ToolResultStatus.ERROR.value == "error"
        assert ToolResultStatus.PARTIAL.value == "partial"

    def test_enum_is_str_subclass(self):
        """StrEnum makes status a str subclass — load-bearing for
        equality with bare strings used in audit-log filters."""
        assert isinstance(ToolResultStatus.OK, str)
        assert ToolResultStatus.OK == "ok"

    def test_str_returns_canonical_lowercase_token(self):
        """``str(status)`` and f-string interpolation must yield the
        bare wire token, not ``ToolResultStatus.OK``. With a plain
        ``(str, Enum)`` mix-in this would fail on Python 3.11+; we use
        ``StrEnum`` deliberately. The honesty audit hook's regex
        compares against this rendering."""
        assert str(ToolResultStatus.OK) == "ok"
        assert str(ToolResultStatus.ERROR) == "error"
        assert str(ToolResultStatus.PARTIAL) == "partial"
        assert f"{ToolResultStatus.OK}" == "ok"

    def test_only_three_states(self):
        """Adding a state is a contract change. This test guards
        against silently extending the lifecycle."""
        assert {s.value for s in ToolResultStatus} == {"ok", "error", "partial"}


# ---------------------------------------------------------------------------
# Factory: ToolResult.ok
# ---------------------------------------------------------------------------


class TestToolResultOk:
    def test_minimal_ok_construction(self):
        r = ToolResult.ok("Saved fact 12345")
        assert r.status is ToolResultStatus.OK
        assert r.confirmation == "Saved fact 12345"
        assert r.error is None
        assert r.data is None

    def test_ok_with_data(self):
        r = ToolResult.ok(
            "Saved fact 12345",
            data={"node_id": "12345", "size_bytes": 42},
        )
        assert r.status is ToolResultStatus.OK
        assert r.data == {"node_id": "12345", "size_bytes": 42}

    def test_ok_rejects_empty_confirmation(self):
        with pytest.raises(ValueError, match="non-empty confirmation"):
            ToolResult.ok("")

    def test_ok_rejects_explicit_error_via_constructor(self):
        """Direct construction with status=OK + error must still raise.
        Factories aren't the only entry point; callers can hit __init__.
        """
        with pytest.raises(ValueError, match="cannot carry an error"):
            ToolResult(
                status=ToolResultStatus.OK,
                confirmation="ok",
                error="actually failed",
            )


# ---------------------------------------------------------------------------
# Factory: ToolResult.failed (renamed from .error to avoid shadowing the
# `error` field — see ToolResult.failed's docstring)
# ---------------------------------------------------------------------------


class TestToolResultFailed:
    def test_minimal_error_construction(self):
        r = ToolResult.failed("memory store returned no node_id")
        assert r.status is ToolResultStatus.ERROR
        assert r.error == "memory store returned no node_id"
        assert r.confirmation is None
        assert r.data is None

    def test_error_with_data(self):
        r = ToolResult.failed(
            "writeback timed out",
            data={"attempted_path": "/tmp/foo", "elapsed_ms": 5000},
        )
        assert r.status is ToolResultStatus.ERROR
        assert r.data == {"attempted_path": "/tmp/foo", "elapsed_ms": 5000}

    def test_error_rejects_empty_message(self):
        with pytest.raises(ValueError, match="non-empty error"):
            ToolResult.failed("")

    def test_error_rejects_explicit_confirmation_via_constructor(self):
        with pytest.raises(ValueError, match="cannot carry a confirmation"):
            ToolResult(
                status=ToolResultStatus.ERROR,
                error="failed",
                confirmation="actually saved",
            )


# ---------------------------------------------------------------------------
# Factory: ToolResult.partial
# ---------------------------------------------------------------------------


class TestToolResultPartial:
    def test_minimal_partial_construction(self):
        r = ToolResult.partial(
            "Saved fact 12345",
            "Index update queued but not yet applied",
        )
        assert r.status is ToolResultStatus.PARTIAL
        assert r.confirmation == "Saved fact 12345"
        assert r.error == "Index update queued but not yet applied"
        assert r.data is None

    def test_partial_requires_both_confirmation_and_error(self):
        # Missing confirmation
        with pytest.raises(ValueError, match="requires a non-empty confirmation"):
            ToolResult(
                status=ToolResultStatus.PARTIAL,
                confirmation=None,
                error="caveat",
            )
        # Missing error
        with pytest.raises(ValueError, match="requires a non-empty error"):
            ToolResult(
                status=ToolResultStatus.PARTIAL,
                confirmation="ok",
                error=None,
            )


# ---------------------------------------------------------------------------
# Type guards
# ---------------------------------------------------------------------------


class TestToolResultTypeGuards:
    def test_status_must_be_enum_or_canonical_str(self):
        """We accept the string value (caller convenience) but reject
        anything else — including unrelated strings."""
        # Canonical str coerces — call site convenience.
        r = ToolResult(status="ok", confirmation="hi")
        assert r.status is ToolResultStatus.OK

        # Unknown string raises with a helpful message.
        with pytest.raises(ValueError, match="must be a ToolResultStatus"):
            ToolResult(status="kinda-ok", confirmation="hi")

        # Non-string non-enum types are TypeErrors (programmer error).
        with pytest.raises(TypeError, match="must be a ToolResultStatus"):
            ToolResult(status=1, confirmation="hi")

    def test_confirmation_must_be_str_or_none(self):
        with pytest.raises(TypeError, match="confirmation must be a str"):
            ToolResult.ok(confirmation=123)  # type: ignore[arg-type]

    def test_error_must_be_str_or_none(self):
        with pytest.raises(TypeError, match="error must be a str"):
            ToolResult.failed(error={"msg": "x"})  # type: ignore[arg-type]

    def test_data_must_be_dict_or_none(self):
        with pytest.raises(TypeError, match="data must be a dict"):
            ToolResult.ok("ok", data=["not", "a", "dict"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Frozen / immutability
# ---------------------------------------------------------------------------


class TestToolResultFrozen:
    def test_cannot_mutate_status(self):
        r = ToolResult.ok("done")
        with pytest.raises(Exception):  # FrozenInstanceError subclasses Exception
            r.status = ToolResultStatus.ERROR  # type: ignore[misc]

    def test_cannot_add_unknown_attribute(self):
        """frozen=True means assignments raise — including new attrs."""
        r = ToolResult.ok("done")
        with pytest.raises(Exception):
            r.extra_field = "leaked"  # type: ignore[attr-defined]

    def test_equal_results_are_equal(self):
        """frozen + dataclass = structural equality, useful in tests."""
        a = ToolResult.ok("Saved", data={"id": "x"})
        b = ToolResult.ok("Saved", data={"id": "x"})
        assert a == b


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestToolResultSerialization:
    def test_to_dict_ok_omits_none(self):
        r = ToolResult.ok("Saved")
        assert r.to_dict() == {"status": "ok", "confirmation": "Saved"}

    def test_to_dict_error_omits_none(self):
        r = ToolResult.failed("network down")
        assert r.to_dict() == {"status": "error", "error": "network down"}

    def test_to_dict_partial_includes_both(self):
        r = ToolResult.partial("Saved with degraded indexing", "indexing queued")
        assert r.to_dict() == {
            "status": "partial",
            "confirmation": "Saved with degraded indexing",
            "error": "indexing queued",
        }

    def test_to_dict_with_data(self):
        r = ToolResult.ok("Saved", data={"node_id": "42"})
        assert r.to_dict() == {
            "status": "ok",
            "confirmation": "Saved",
            "data": {"node_id": "42"},
        }

    def test_status_value_is_lowercase_string(self):
        """Wire format must round-trip through json.dumps."""
        import json

        out = json.dumps(ToolResult.ok("Saved").to_dict())
        assert '"status": "ok"' in out


# ---------------------------------------------------------------------------
# Honesty layer rationale (regression guards for #1042 layer 4)
# ---------------------------------------------------------------------------


class TestHonestyLayerInvariants:
    """These tests pin the contract the honesty audit hook depends on.

    Touching them is a contract change; coordinate with the audit
    hook's narration check before you do.
    """

    def test_ok_implies_no_error_field(self):
        """Honesty hook: ``status == 'ok'`` is the green light to allow
        a "Saved!" claim to stand. If a future change lets ``error``
        coexist with status=OK, the hook would let confident lies
        through. Pin it."""
        r = ToolResult.ok("Saved")
        assert r.error is None

    def test_error_implies_no_confirmation_field(self):
        """Mirror of the above: status=ERROR must never carry a
        confirmation. Otherwise the LLM could read ``confirmation``
        and claim success despite the error."""
        r = ToolResult.failed("failed")
        assert r.confirmation is None

    def test_partial_carries_both_so_narration_must_surface_both(self):
        """The whole point of PARTIAL: the LLM is contractually obliged
        to surface BOTH halves to the user. The presence of both
        fields is what makes that obligation enforceable."""
        r = ToolResult.partial("Saved", "indexing degraded")
        assert r.confirmation
        assert r.error


class TestToolResultParts:
    """First-class typed render parts on the envelope (sovereign #2641)."""

    def test_parts_default_is_none(self):
        assert ToolResult.ok("Saved").parts is None

    def test_factories_accept_parts(self):
        entry = {"type": "selfie_finished", "data": {"url": "u"}, "id": "s1"}
        assert ToolResult.ok("Saved", parts=[entry]).parts == [entry]
        assert ToolResult.failed("boom", parts=[entry]).parts == [entry]
        assert ToolResult.partial("Saved", "degraded", parts=[entry]).parts == [entry]

    def test_to_dict_carries_parts(self):
        entry = {"type": "selfie_finished", "data": {"url": "u"}}
        assert ToolResult.ok("Saved", parts=[entry]).to_dict()["parts"] == [entry]

    def test_to_dict_omits_absent_or_empty_parts(self):
        """No parts serializes to the exact pre-parts envelope shape."""
        assert "parts" not in ToolResult.ok("Saved").to_dict()
        assert "parts" not in ToolResult.ok("Saved", parts=[]).to_dict()

    def test_parts_must_be_a_list(self):
        with pytest.raises(TypeError):
            ToolResult.ok("Saved", parts={"type": "t"})

    def test_parts_entries_must_be_dicts(self):
        with pytest.raises(TypeError):
            ToolResult.ok("Saved", parts=["not-a-dict"])

    def test_parts_entries_require_nonempty_string_type(self):
        with pytest.raises(ValueError):
            ToolResult.ok("Saved", parts=[{"data": 1}])
        with pytest.raises(ValueError):
            ToolResult.ok("Saved", parts=[{"type": "", "data": 1}])
        with pytest.raises(ValueError):
            ToolResult.ok("Saved", parts=[{"type": 7, "data": 1}])

    def test_deep_sanitization_is_not_the_sdk_job(self):
        """Size caps / control-character rules live in the framework at
        the dispatch boundary — a structurally-valid but wire-invalid
        entry constructs fine here and is dropped there. Pin the split
        so the rules never get duplicated."""
        r = ToolResult.ok("Saved", parts=[{"type": "bad\x1etype", "data": 1}])
        assert r.parts[0]["type"] == "bad\x1etype"
