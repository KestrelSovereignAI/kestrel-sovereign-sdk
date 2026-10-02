"""Decision contract: validation, immutable snapshot, adapter defaults."""

from __future__ import annotations

import asyncio
import json
import math

import pytest

from kestrel_sdk.llm import (
    ChoiceQuestion,
    DecisionModelInfo,
    DecisionRequest,
    DecisionRequestInvalid,
    DecisionsNotSupported,
    LLMAdapter,
    NoulQuestion,
    ProviderCapabilities,
    ScoreQuestion,
    concentration,
    validate_decision_request,
)
from kestrel_sdk.llm.decisions import (
    MAX_DESCRIPTION_CHARS,
    MAX_INSTRUCTIONS_CHARS,
    MAX_OPTIONS,
    MAX_QUESTIONS,
    MAX_REQUEST_BYTES,
    MAX_STATE_DEPTH,
)


def _choice(**options: str | None) -> ChoiceQuestion:
    return ChoiceQuestion(
        instructions="Which team should handle `ticket`?",
        options=options or {"billing": "Payments", "technical": None},
    )


def _request(state: object = None, **questions: object) -> DecisionRequest:
    return DecisionRequest(
        state={"ticket": "charged twice"} if state is None else state,
        questions=questions or {"team": _choice()},  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Snapshot and wire form
# ---------------------------------------------------------------------------


def test_snapshot_is_immune_to_caller_mutation_after_validation() -> None:
    state = {"ticket": "charged twice", "history": ["a"]}
    options = {"billing": "Payments", "technical": "Bugs"}
    levels = ["Routine", "Urgent"]
    request = DecisionRequest(
        state=state,
        questions={
            "team": ChoiceQuestion(instructions="Which team?", options=options),
            "urgency": ScoreQuestion(instructions="How urgent?", levels=levels),
        },
    )
    snapshot = validate_decision_request(request)
    before = snapshot.canonical_json

    state["ticket"] = "ignore previous instructions"
    state["history"].append("b")  # type: ignore[attr-defined]
    options["sales"] = "Sales"
    levels.append("Critical")

    assert snapshot.canonical_json == before
    assert snapshot.state["ticket"] == "charged twice"  # type: ignore[index]
    assert snapshot.state["history"] == ("a",)  # type: ignore[index]
    assert set(snapshot.questions["team"].options) == {"billing", "technical"}  # type: ignore[union-attr]
    assert snapshot.questions["urgency"].levels == ("Routine", "Urgent")  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        snapshot.questions["team"].options["x"] = None  # type: ignore[index,union-attr]


def test_wire_form_is_the_systemone_shape() -> None:
    snapshot = validate_decision_request(
        _request(
            team=_choice(billing="Payments", other=None),
            urgency=ScoreQuestion(instructions="How urgent?", levels=["Low", "High"]),
            refund=NoulQuestion(instructions="Does the customer ask for a refund?"),
            fraud=NoulQuestion(
                instructions="Is this fraud?", true_means="stolen card"
            ),
        )
    )
    wire = json.loads(snapshot.canonical_json)

    assert wire["state"] == {"ticket": "charged twice"}
    assert wire["questions"]["team"] == {
        "type": "choice",
        "instructions": "Which team should handle `ticket`?",
        "criteria": {"billing": "Payments", "other": None},
    }
    assert wire["questions"]["urgency"]["criteria"] == ["Low", "High"]
    assert "criteria" not in wire["questions"]["refund"]
    assert wire["questions"]["fraud"]["criteria"] == {
        "true": "stolen card",
        "false": None,
    }
    assert list(wire["questions"]) == ["team", "urgency", "refund", "fraud"]
    assert snapshot.encoded_bytes == len(snapshot.canonical_json)


# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------


def _rule(request: DecisionRequest) -> str:
    with pytest.raises(DecisionRequestInvalid) as excinfo:
        validate_decision_request(request)
    return excinfo.value.rule


def test_question_count_bounds() -> None:
    assert _rule(DecisionRequest(state="s", questions={})) == "questions"
    too_many = {f"q{i}": NoulQuestion(instructions="ok?") for i in range(MAX_QUESTIONS + 1)}
    assert _rule(DecisionRequest(state="s", questions=too_many)) == "questions"
    exactly = {f"q{i}": NoulQuestion(instructions="ok?") for i in range(MAX_QUESTIONS)}
    validate_decision_request(DecisionRequest(state="s", questions=exactly))


@pytest.mark.parametrize("bad_id", ["", "-lead", "has space", "a" * 65, "x\n", 3])
def test_question_and_option_ids_are_restricted(bad_id: object) -> None:
    question_map = {bad_id: NoulQuestion(instructions="ok?")}
    assert _rule(DecisionRequest(state="s", questions=question_map)) == "id"  # type: ignore[dict-item]
    choice = ChoiceQuestion(instructions="pick", options={bad_id: None, "ok": None})  # type: ignore[dict-item]
    assert _rule(_request(team=choice)) == "id"


def test_option_and_level_cardinality() -> None:
    assert _rule(_request(team=ChoiceQuestion(instructions="pick", options={"only": None}))) == "options"
    many = {f"o{i}": None for i in range(MAX_OPTIONS + 1)}
    assert _rule(_request(team=ChoiceQuestion(instructions="pick", options=many))) == "options"
    assert _rule(_request(level=ScoreQuestion(instructions="rate", levels=["one"]))) == "options"
    assert _rule(_request(level=ScoreQuestion(instructions="rate", levels="ab"))) == "options"  # type: ignore[arg-type]


def test_text_rules() -> None:
    assert _rule(_request(q=NoulQuestion(instructions="   "))) == "text"
    assert _rule(_request(q=NoulQuestion(instructions="x" * (MAX_INSTRUCTIONS_CHARS + 1)))) == "text"
    long_description = "d" * (MAX_DESCRIPTION_CHARS + 1)
    assert _rule(_request(team=_choice(a=long_description, b=None))) == "text"
    assert _rule(_request(level=ScoreQuestion(instructions="rate", levels=["low", ""]))) == "text"
    assert _rule(_request(q=NoulQuestion(instructions="ok?", true_means=""))) == "text"


def test_unknown_question_type_is_rejected() -> None:
    assert _rule(_request(q={"type": "noul", "instructions": "raw dict"})) == "type"


@pytest.mark.parametrize(
    ("state", "rule"),
    [
        ({1: "a", "1": "b"}, "key"),
        ({"x": float("nan")}, "number"),
        ({"x": float("inf")}, "number"),
        ({"when": object()}, "type"),
    ],
)
def test_state_must_be_a_strict_json_tree(state: object, rule: str) -> None:
    assert _rule(_request(state=state)) == rule


def test_state_cycles_and_depth_are_rejected() -> None:
    looped: dict[str, object] = {}
    looped["self"] = looped
    assert _rule(_request(state=looped)) == "cycle"

    nested: object = "leaf"
    for _ in range(MAX_STATE_DEPTH + 1):
        nested = {"n": nested}
    assert _rule(_request(state=nested)) == "depth"

    ok: object = "leaf"
    for _ in range(MAX_STATE_DEPTH - 1):
        ok = {"n": ok}
    validate_decision_request(_request(state=ok))


def test_whole_request_size_cap() -> None:
    assert _rule(_request(state="x" * MAX_REQUEST_BYTES)) == "bytes"
    snapshot = validate_decision_request(_request(state="x" * 1000))
    assert snapshot.encoded_bytes < MAX_REQUEST_BYTES


def test_non_request_is_rejected() -> None:
    with pytest.raises(DecisionRequestInvalid):
        validate_decision_request({"state": "s"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Concentration
# ---------------------------------------------------------------------------


def test_concentration_bounds_and_shape() -> None:
    assert concentration([0.5, 0.5]) == pytest.approx(0.0)
    assert concentration([1.0, 0.0, 0.0]) == pytest.approx(1.0)
    peaked = concentration([0.9, 0.05, 0.05])
    flatter = concentration([0.6, 0.2, 0.2])
    assert 0.0 < flatter < peaked < 1.0
    expected = 1 - (-(0.9 * math.log(0.9) + 2 * 0.05 * math.log(0.05))) / math.log(3)
    assert peaked == pytest.approx(expected)
    with pytest.raises(ValueError):
        concentration([1.0])


# ---------------------------------------------------------------------------
# Adapter and capability surface
# ---------------------------------------------------------------------------


class _ChatOnly(LLMAdapter):
    async def get_response(self, client, model, messages, **kwargs):  # type: ignore[override]
        raise NotImplementedError


def test_adapter_defaults_mean_not_supported() -> None:
    adapter = _ChatOnly()
    snapshot = validate_decision_request(_request())

    with pytest.raises(DecisionsNotSupported):
        asyncio.run(adapter.adecide(None, "m", snapshot, timeout=1.0))
    assert asyncio.run(adapter.list_decision_models(None)) == []
    assert adapter.provider_capabilities().supports_decisions is False


def test_supports_decisions_round_trips_through_mapping() -> None:
    capabilities = ProviderCapabilities(supports_decisions=True)
    data = capabilities.to_dict()
    assert data["supports_decisions"] is True
    assert ProviderCapabilities.from_mapping(data).supports_decisions is True
    assert ProviderCapabilities.from_mapping({}).supports_decisions is False


def test_decision_model_info_defaults() -> None:
    info = DecisionModelInfo(id="m", vendor="v", route="v:r")
    assert info.context_limit is None
    assert info.parallel_questions is None


@pytest.mark.parametrize(
    "request_",
    [
        _request(state={"x": "\ud800"}),
        _request(state={"\udfff": 1}),
        _request(q=NoulQuestion(instructions="ok\ud800?")),
        _request(team=ChoiceQuestion(instructions="pick", options={"a": "\ud800", "b": None})),
    ],
)
def test_invalid_unicode_is_a_validation_error(request_: DecisionRequest) -> None:
    with pytest.raises(DecisionRequestInvalid) as excinfo:
        validate_decision_request(request_)
    assert excinfo.value.rule == "text"


def test_huge_integer_is_a_validation_error() -> None:
    assert _rule(_request(state={"n": 10**5000})) == "number"
