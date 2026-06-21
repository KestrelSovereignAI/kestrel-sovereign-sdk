"""Tests for the Waitable provider contract.

This is the SDK-level teaching contract every "wait" feature implements
so the generic wait engine (in core) can drive its poll loop. The four
``Outcome`` values are the single terminal vocabulary that replaces the
divergent per-feature sets; the shape rules here are what the engine and
external providers rely on. A change here is a contract change.
"""

from __future__ import annotations

import pytest

from kestrel_sdk.tools import Outcome, WaitStatus, Waitable
from kestrel_sdk.tools.waitable import Outcome as OutcomeDirect


# ---------------------------------------------------------------------------
# Outcome enum
# ---------------------------------------------------------------------------


class TestOutcome:
    def test_values_are_canonical_lowercase(self):
        assert Outcome.PENDING.value == "pending"
        assert Outcome.DONE.value == "done"
        assert Outcome.FAILED.value == "failed"
        assert Outcome.PARTIAL.value == "partial"

    def test_is_str_subclass(self):
        """StrEnum — bare-token interpolation and string equality."""
        assert Outcome.DONE == "done"
        assert f"{Outcome.DONE}" == "done"

    def test_reexported_from_package_root(self):
        assert Outcome is OutcomeDirect

    def test_only_pending_is_non_terminal(self):
        assert Outcome.PENDING.is_terminal() is False
        assert Outcome.DONE.is_terminal() is True
        assert Outcome.FAILED.is_terminal() is True
        assert Outcome.PARTIAL.is_terminal() is True


# ---------------------------------------------------------------------------
# WaitStatus envelope
# ---------------------------------------------------------------------------


class TestWaitStatus:
    def test_minimal_construction(self):
        s = WaitStatus(Outcome.PENDING, "still running")
        assert s.outcome is Outcome.PENDING
        assert s.summary == "still running"
        assert s.data is None

    def test_carries_data_payload(self):
        s = WaitStatus(Outcome.DONE, "job complete", data={"rc": 0})
        assert s.data == {"rc": 0}

    def test_frozen(self):
        s = WaitStatus(Outcome.DONE, "done")
        with pytest.raises(Exception):
            s.summary = "mutated"  # type: ignore[misc]

    def test_string_outcome_is_coerced(self):
        s = WaitStatus("done", "ok")
        assert s.outcome is Outcome.DONE

    def test_unknown_string_outcome_rejected(self):
        with pytest.raises(ValueError):
            WaitStatus("finished_unknown", "legacy talon token")

    def test_non_enum_non_str_outcome_rejected(self):
        with pytest.raises(TypeError):
            WaitStatus(1, "nope")  # type: ignore[arg-type]

    def test_empty_summary_rejected(self):
        with pytest.raises(ValueError):
            WaitStatus(Outcome.DONE, "")

    def test_non_dict_data_rejected(self):
        with pytest.raises(TypeError):
            WaitStatus(Outcome.DONE, "ok", data=["not", "a", "dict"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Waitable protocol — structural conformance
# ---------------------------------------------------------------------------


class _GoodProvider:
    kind = "demo"
    signal = "demo.complete"

    async def poll(self, handle: str) -> WaitStatus:
        return WaitStatus(Outcome.DONE, f"{handle} done")


class _SignalessProvider:
    kind = "demo2"
    signal = None

    async def poll(self, handle: str) -> WaitStatus:
        return WaitStatus(Outcome.PENDING, "waiting")


class _NotAProvider:
    kind = "broken"
    # missing signal attr and poll method


class TestWaitableProtocol:
    def test_conforming_provider_passes_isinstance(self):
        assert isinstance(_GoodProvider(), Waitable)

    def test_signaless_provider_conforms(self):
        assert isinstance(_SignalessProvider(), Waitable)

    def test_non_conforming_rejected(self):
        assert not isinstance(_NotAProvider(), Waitable)

    @pytest.mark.asyncio
    async def test_poll_returns_waitstatus(self):
        s = await _GoodProvider().poll("job_42")
        assert s.outcome is Outcome.DONE
        assert "job_42" in s.summary


class _MonitorableProvider:
    kind = "mon"
    signal = "mon.complete"

    async def poll(self, handle: str) -> WaitStatus:
        return WaitStatus(Outcome.DONE, handle)

    async def active_handles(self) -> list:
        return ["a", "b"]


class TestMonitorableWaitable:
    def test_monitorable_is_also_a_waitable(self):
        from kestrel_sdk.tools import MonitorableWaitable

        p = _MonitorableProvider()
        assert isinstance(p, MonitorableWaitable)
        assert isinstance(p, Waitable)

    def test_poll_only_provider_is_not_monitorable(self):
        from kestrel_sdk.tools import MonitorableWaitable

        # _GoodProvider implements poll but not active_handles.
        assert isinstance(_GoodProvider(), Waitable)
        assert not isinstance(_GoodProvider(), MonitorableWaitable)

    @pytest.mark.asyncio
    async def test_active_handles_enumerates(self):
        assert await _MonitorableProvider().active_handles() == ["a", "b"]
