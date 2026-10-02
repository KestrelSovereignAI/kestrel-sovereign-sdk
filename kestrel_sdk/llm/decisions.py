"""Decision contract: typed choice / score / noul questions over a state.

A decision request is a ``state`` plus named, independent questions. A decision
model answers each question with a probability distribution instead of text.
The wire shape follows TypeSafe's ``/v1/systemone`` API, which is now served by
several hosted vendors and local runtimes. Kestrel's design is specified in
``docs/architecture/llm/DECISIONS.md`` in the kestrel-sovereign repository.

This module holds everything both sides of the adapter boundary share:

* request, answer and result types;
* :func:`validate_decision_request`, which turns a caller's request into an
  immutable, size-measured :class:`ValidatedDecisionRequest`. Adapters only
  ever receive that snapshot, so a caller mutating its own ``state`` while a
  call is in flight cannot change what is sent;
* the validation bounds, which are the intersection every known route accepts;
* typed exceptions and rejection reasons;
* :func:`concentration`, the single definition of how peaked a distribution
  is. Vendor ``confidence`` fields are deliberately not part of the contract
  because vendors define them differently.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, TypeAlias

from kestrel_sdk._frozen_json import (
    ImmutableJSON,
    JSONLimitError,
    JSONLimits,
    JSONTypeError,
    canonical_json_bytes,
    freeze_json,
)

# ---------------------------------------------------------------------------
# Validation bounds (DECISIONS.md §2.2)
# ---------------------------------------------------------------------------

#: Questions per request. 64 is the smallest published route cap (Ollama), so
#: a valid request fits every known route and is never split.
MAX_QUESTIONS = 64
#: Options per choice question and levels per score question. One option is
#: not a decision; 26 is the smallest published cap (Ollama letter scoring).
MIN_OPTIONS = 2
MAX_OPTIONS = 26
MAX_INSTRUCTIONS_CHARS = 4096
MAX_DESCRIPTION_CHARS = 1024
#: Canonical JSON size of the whole request (state and questions).
MAX_REQUEST_BYTES = 1 << 20
#: Nesting depth of ``state``.
MAX_STATE_DEPTH = 64

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


# ---------------------------------------------------------------------------
# Questions and request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChoiceQuestion:
    """Which of these named options fits the state?

    ``options`` maps option id to a description; ``None`` lets the id describe
    itself.
    """

    instructions: str
    options: Mapping[str, str | None]


@dataclass(frozen=True)
class ScoreQuestion:
    """Where does the state sit on this ordered rubric? Lowest level first."""

    instructions: str
    levels: Sequence[str]


@dataclass(frozen=True)
class NoulQuestion:
    """Is this statement true of the state?"""

    instructions: str
    true_means: str | None = None
    false_means: str | None = None


Question: TypeAlias = ChoiceQuestion | ScoreQuestion | NoulQuestion


@dataclass(frozen=True)
class DecisionRequest:
    """What a caller asks. Validate it with :func:`validate_decision_request`."""

    state: Any
    questions: Mapping[str, Question]


@dataclass(frozen=True)
class ValidatedDecisionRequest:
    """An immutable snapshot of a validated request.

    ``questions`` holds frozen copies (options as read-only mappings, levels as
    tuples). ``wire`` is the canonical systemone-shaped request
    (``{"state": ..., "questions": {...}}``) frozen in the same pass, and
    ``encoded_bytes`` is the exact size of :attr:`canonical_json`.
    """

    state: ImmutableJSON
    questions: Mapping[str, Question]
    wire: Mapping[str, ImmutableJSON]
    encoded_bytes: int

    @property
    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self.wire)


# ---------------------------------------------------------------------------
# Answers and result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    probabilities: tuple[float, ...]


@dataclass(frozen=True)
class NoulAnswer:
    p_true: float


Answer: TypeAlias = ChoiceAnswer | ScoreAnswer | NoulAnswer


@dataclass(frozen=True)
class DecisionResult:
    """A complete, normalised answer set from one model.

    ``thresholds`` and ``calibrated`` are resolved by the service for the model
    that answered: ``calibrated`` is ``False`` when the caller's ``default``
    thresholds were applied and ``None`` when the caller declares no
    thresholds.
    """

    answers: Mapping[str, Answer]
    vendor: str
    route: str
    model: str
    thresholds: Mapping[str, float]
    calibrated: bool | None
    input_tokens: int | None
    duration_ms: int


@dataclass(frozen=True)
class DecisionModelInfo:
    """One decision model a route can serve, with its effective limits.

    ``context_limit`` is the serving limit, not the base model's. ``None`` for
    a cap means the route publishes none; the validation bounds above still
    apply.
    """

    id: str
    vendor: str
    route: str
    context_limit: int | None = None
    max_questions: int | None = None
    max_options: int | None = None
    max_request_bytes: int | None = None
    parallel_questions: bool | None = None
    created_at: str | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DecisionError(Exception):
    """Base class for every decision failure."""


class DecisionsNotSupported(DecisionError):
    """The adapter has no decision surface."""


class DecisionRequestInvalid(DecisionError, ValueError):
    """The request failed validation. Nothing was sent."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


class UnavailableReason(str, Enum):
    DISABLED = "disabled"
    SELECTOR_CONFLICT = "selector_conflict"
    NO_ROUTE = "no_route"
    NO_LOCAL_ROUTE = "no_local_route"
    NO_CANDIDATE = "no_candidate"


class RejectionReason(str, Enum):
    NO_MODELS = "no_models"
    AMBIGUOUS_MODEL = "ambiguous_model"
    NOT_CALIBRATED = "not_calibrated"
    NO_FIT = "no_fit"
    UNVERIFIED_PIN = "unverified_pin"
    NOT_SERVED = "not_served"
    PIN_CONFLICT = "pin_conflict"


@dataclass(frozen=True)
class RouteRejection:
    route: str
    reason: RejectionReason
    model: str | None = None
    detail: str | None = None


class DecisionUnavailable(DecisionError):
    """No route could take the request. Nothing was sent."""

    def __init__(
        self,
        reason: UnavailableReason,
        message: str,
        *,
        rejections: Sequence[RouteRejection] = (),
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.rejections = tuple(rejections)


class DecisionTimeout(DecisionError):
    """The caller's deadline elapsed."""


class DecisionTransportError(DecisionError):
    """Network or HTTP failure reaching the route, including a missing model."""


class DecisionProtocolError(DecisionError):
    """The route answered, but the answer violates the contract."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _check_id(value: object, what: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise DecisionRequestInvalid(
            "id",
            f"{what} must match {_ID_PATTERN.pattern!r}, got {value!r}",
        )
    return value


def _check_text(value: object, what: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionRequestInvalid("text", f"{what} must be a non-empty string")
    if len(value) > max_chars:
        raise DecisionRequestInvalid(
            "text", f"{what} must be at most {max_chars} characters"
        )
    return value


def _check_optional_text(value: object, what: str) -> str | None:
    if value is None:
        return None
    return _check_text(value, what, MAX_DESCRIPTION_CHARS)


def _check_cardinality(count: int, what: str) -> None:
    if not MIN_OPTIONS <= count <= MAX_OPTIONS:
        raise DecisionRequestInvalid(
            "options",
            f"{what} must have between {MIN_OPTIONS} and {MAX_OPTIONS} entries, "
            f"got {count}",
        )


def _freeze_question(question_id: str, question: object) -> tuple[Question, dict[str, Any]]:
    """Return a frozen question and its systemone wire form."""

    where = f"question {question_id!r}"
    if isinstance(question, ChoiceQuestion):
        instructions = _check_text(
            question.instructions, f"{where} instructions", MAX_INSTRUCTIONS_CHARS
        )
        if not isinstance(question.options, Mapping):
            raise DecisionRequestInvalid("options", f"{where} options must be a mapping")
        _check_cardinality(len(question.options), f"{where} options")
        options: dict[str, str | None] = {}
        for option_id, description in question.options.items():
            _check_id(option_id, f"{where} option id")
            options[option_id] = _check_optional_text(
                description, f"{where} option {option_id!r} description"
            )
        frozen: Question = ChoiceQuestion(
            instructions=instructions, options=MappingProxyType(options)
        )
        wire = {"type": "choice", "instructions": instructions, "criteria": options}
        return frozen, wire
    if isinstance(question, ScoreQuestion):
        instructions = _check_text(
            question.instructions, f"{where} instructions", MAX_INSTRUCTIONS_CHARS
        )
        if isinstance(question.levels, (str, bytes)) or not isinstance(
            question.levels, Sequence
        ):
            raise DecisionRequestInvalid("options", f"{where} levels must be a sequence")
        _check_cardinality(len(question.levels), f"{where} levels")
        levels = tuple(
            _check_text(level, f"{where} level {index}", MAX_DESCRIPTION_CHARS)
            for index, level in enumerate(question.levels)
        )
        frozen = ScoreQuestion(instructions=instructions, levels=levels)
        wire = {"type": "score", "instructions": instructions, "criteria": list(levels)}
        return frozen, wire
    if isinstance(question, NoulQuestion):
        instructions = _check_text(
            question.instructions, f"{where} instructions", MAX_INSTRUCTIONS_CHARS
        )
        true_means = _check_optional_text(question.true_means, f"{where} true_means")
        false_means = _check_optional_text(question.false_means, f"{where} false_means")
        frozen = NoulQuestion(
            instructions=instructions, true_means=true_means, false_means=false_means
        )
        wire = {"type": "noul", "instructions": instructions}
        if true_means is not None or false_means is not None:
            wire["criteria"] = {"true": true_means, "false": false_means}
        return frozen, wire
    raise DecisionRequestInvalid(
        "type",
        f"{where} must be a ChoiceQuestion, ScoreQuestion or NoulQuestion, "
        f"not {type(question).__name__}",
    )


def validate_decision_request(request: DecisionRequest) -> ValidatedDecisionRequest:
    """Validate ``request`` and return its immutable, measured snapshot.

    Text and cardinality rules are checked first; then the canonical wire form
    (state plus questions) is frozen and measured in one bounded pass that
    stops as soon as it exceeds :data:`MAX_REQUEST_BYTES`. Every violation
    raises :class:`DecisionRequestInvalid` naming the rule.
    """

    if not isinstance(request, DecisionRequest):
        raise DecisionRequestInvalid(
            "type", f"expected DecisionRequest, not {type(request).__name__}"
        )
    if not isinstance(request.questions, Mapping):
        raise DecisionRequestInvalid("questions", "questions must be a mapping")
    count = len(request.questions)
    if not 1 <= count <= MAX_QUESTIONS:
        raise DecisionRequestInvalid(
            "questions",
            f"a request must have between 1 and {MAX_QUESTIONS} questions, got {count}",
        )

    questions: dict[str, Question] = {}
    wire_questions: dict[str, Any] = {}
    for question_id, question in request.questions.items():
        _check_id(question_id, "question id")
        questions[question_id], wire_questions[question_id] = _freeze_question(
            question_id, question
        )

    try:
        frozen = freeze_json(
            {"state": request.state, "questions": wire_questions},
            path="decision request",
            # The wire root is depth 0 and ``state`` sits at depth 1.
            limits=JSONLimits(
                max_depth=MAX_STATE_DEPTH + 1, max_encoded_bytes=MAX_REQUEST_BYTES
            ),
        )
    except (JSONTypeError, JSONLimitError) as error:
        raise DecisionRequestInvalid(error.rule, str(error)) from error

    wire = frozen.value
    assert isinstance(wire, Mapping) and frozen.encoded_bytes is not None
    return ValidatedDecisionRequest(
        state=wire["state"],
        questions=MappingProxyType(questions),
        wire=wire,
        encoded_bytes=frozen.encoded_bytes,
    )


# ---------------------------------------------------------------------------
# Concentration
# ---------------------------------------------------------------------------


def concentration(probabilities: Sequence[float]) -> float:
    """``1 − H(p)/ln K``: 0 for a uniform distribution, 1 for a one-hot one.

    This is how peaked the distribution is, not the probability that the
    answer is right. ``probabilities`` must have at least two entries and sum
    to 1 (a normalised answer always does).
    """

    values = tuple(probabilities)
    if len(values) < 2:
        raise ValueError("concentration needs at least two probabilities")
    entropy = -sum(p * math.log(p) for p in values if p > 0)
    return max(0.0, min(1.0, 1.0 - entropy / math.log(len(values))))


__all__ = [
    "Answer",
    "ChoiceAnswer",
    "ChoiceQuestion",
    "DecisionError",
    "DecisionModelInfo",
    "DecisionProtocolError",
    "DecisionRequest",
    "DecisionRequestInvalid",
    "DecisionResult",
    "DecisionTimeout",
    "DecisionTransportError",
    "DecisionUnavailable",
    "DecisionsNotSupported",
    "MAX_DESCRIPTION_CHARS",
    "MAX_INSTRUCTIONS_CHARS",
    "MAX_OPTIONS",
    "MAX_QUESTIONS",
    "MAX_REQUEST_BYTES",
    "MAX_STATE_DEPTH",
    "MIN_OPTIONS",
    "NoulAnswer",
    "NoulQuestion",
    "Question",
    "RejectionReason",
    "RouteRejection",
    "ScoreAnswer",
    "ScoreQuestion",
    "UnavailableReason",
    "ValidatedDecisionRequest",
    "concentration",
    "validate_decision_request",
]
