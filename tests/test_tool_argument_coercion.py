"""Tool arguments are coerced to the type their schema declares (issue #78).

``@tool`` advertises ``max_results`` as an ``integer``, but a model is free to
send the JSON string ``"30"``. That string used to reach the feature method
untouched, so ``min(per_page, 100)`` inside the feature raised
``'<' not supported between instances of 'int' and 'str'``.

The method-wrapping ``DynamicTool.execute`` is the one door every JSON-argument
call passes through and the only place that knows the declared type, so it
coerces there — reusing the command-prefix path's rules so the two cannot
disagree. Values that cannot be coerced come back as a failed ``ToolResult``
naming the parameter and the expected type instead of a ``TypeError`` raised
from inside the feature.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from kestrel_sdk.features.base import Feature, tool
from kestrel_sdk.tools.base import (
    COERCION_FAILED,
    AgentTool,
    ToolCategory,
    ToolSchema,
    coerce_json_value,
)
from kestrel_sdk.tools.result import ToolResult


class _CoercionFeature(Feature):
    """Feature whose tools report the exact types that reached the method."""

    def __init__(self, agent=None):
        super().__init__(agent)
        self.calls: List[Dict[str, Any]] = []

    @property
    def tool_description(self) -> str:  # noqa: D401  (single-line)
        return "fixture feature for argument-coercion tests"

    async def initialize(self) -> None:
        """No-op; the fixture doesn't need real lifecycle setup."""
        return None

    @tool(
        name="get_comments",
        description="The shape from the bug report: an int with a default",
        category=ToolCategory.UTILITY,
        command_prefix="!get-comments",
    )
    async def get_comments(self, issue: str, max_results: int = 30) -> ToolResult:
        """
        Fetch comments.

        Args:
            issue: Issue reference
            max_results: How many comments to return
        """
        self.calls.append({"issue": issue, "max_results": max_results})
        # The exact door from the bug report: kestrel-feature-github does
        # ``min(per_page, 100)`` on the value the model supplied.
        per_page = min(max_results, 100)
        return ToolResult.ok(
            f"fetched {per_page}",
            data={
                "per_page": per_page,
                "max_results_type": type(max_results).__name__,
                "issue_type": type(issue).__name__,
            },
        )

    @tool(
        name="scale",
        description="A float parameter",
        category=ToolCategory.UTILITY,
    )
    async def scale(self, factor: float) -> ToolResult:
        """
        Scale something.

        Args:
            factor: The scale factor
        """
        return ToolResult.ok(
            "scaled",
            data={"factor": factor, "type": type(factor).__name__},
        )

    @tool(
        name="toggle",
        description="A boolean parameter",
        category=ToolCategory.UTILITY,
    )
    async def toggle(self, enabled: bool) -> ToolResult:
        """
        Toggle something.

        Args:
            enabled: Whether it is on
        """
        return ToolResult.ok(
            "toggled",
            data={"enabled": enabled, "type": type(enabled).__name__},
        )

    @tool(
        name="structured",
        description="String, array and object parameters",
        category=ToolCategory.UTILITY,
    )
    async def structured(
        self,
        label: str,
        tags: List[str],
        options: Dict[str, Any],
    ) -> ToolResult:
        """
        Accept structured values.

        Args:
            label: A plain string
            tags: A list
            options: A mapping
        """
        return ToolResult.ok(
            "structured",
            data={"label": label, "tags": tags, "options": options},
        )

    @tool(
        name="optional_limit",
        description="An optional integer that may arrive as JSON null",
        category=ToolCategory.UTILITY,
    )
    async def optional_limit(self, limit: Optional[int] = None) -> ToolResult:
        """
        Accept an optional limit.

        Args:
            limit: How many, or None for all
        """
        return ToolResult.ok(
            "limited",
            data={"limit": limit, "type": type(limit).__name__},
        )

    @tool(
        name="implicit_optional_limit",
        description="PEP 484 implicit Optional: a None default without Optional[]",
        category=ToolCategory.UTILITY,
    )
    async def implicit_optional_limit(self, limit: int = None) -> ToolResult:
        """
        Accept an optional limit written the implicit-Optional way.

        Args:
            limit: How many, or None for all
        """
        return ToolResult.ok(
            "limited",
            data={"limit": limit, "type": type(limit).__name__},
        )

    @tool(
        name="required_count",
        description="A required, non-nullable integer",
        category=ToolCategory.UTILITY,
    )
    async def required_count(self, count: int) -> ToolResult:
        """
        Do arithmetic on a required count.

        Args:
            count: How many
        """
        self.calls.append({"count": count})
        # The same door as the bug report, reached by JSON ``null`` instead of
        # a string: ``min(None, 100)`` raises the identical TypeError.
        return ToolResult.ok("counted", data={"capped": min(count, 100)})


@pytest.fixture
def feature() -> _CoercionFeature:
    return _CoercionFeature()


def _dyn_tool(feature: Feature, name: str) -> AgentTool:
    for candidate in feature.get_tools():
        if candidate.name == name:
            return candidate
    raise AssertionError(
        f"tool {name!r} not found among {[t.name for t in feature.get_tools()]}"
    )


class TestJsonArgumentPath:
    """The model's decoded JSON arguments reach the method correctly typed."""

    @pytest.mark.asyncio
    async def test_model_sent_string_integer_arrives_as_int(self, feature):
        out = await _dyn_tool(feature, "get_comments").execute(
            issue="#78", max_results="30"
        )
        assert out["status"] == "ok"
        assert out["data"]["max_results_type"] == "int"
        assert feature.calls == [{"issue": "#78", "max_results": 30}]

    @pytest.mark.asyncio
    async def test_the_exact_min_door_from_the_bug_report(self, feature):
        """``min("30", 100)`` raised before; now the comparison succeeds."""
        out = await _dyn_tool(feature, "get_comments").execute(
            issue="#78", max_results="30"
        )
        assert out["status"] == "ok"
        assert out["data"]["per_page"] == 30
        assert "not supported between instances" not in str(out)

    @pytest.mark.asyncio
    async def test_correctly_typed_integer_passes_through(self, feature):
        out = await _dyn_tool(feature, "get_comments").execute(
            issue="#78", max_results=250
        )
        assert out["data"]["per_page"] == 100
        assert out["data"]["max_results_type"] == "int"

    @pytest.mark.asyncio
    async def test_omitted_parameter_keeps_its_default(self, feature):
        out = await _dyn_tool(feature, "get_comments").execute(issue="#78")
        assert out["data"]["per_page"] == 30
        assert feature.calls == [{"issue": "#78", "max_results": 30}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sent,expected",
        [("1.5", 1.5), (1.5, 1.5), ("2", 2.0), (2, 2.0), (2.0, 2.0)],
    )
    async def test_number_parameters_arrive_as_float(self, feature, sent, expected):
        out = await _dyn_tool(feature, "scale").execute(factor=sent)
        assert out["data"]["factor"] == expected
        assert out["data"]["type"] == "float"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sent,expected",
        [
            ("true", True),
            ("True", True),
            ("yes", True),
            ("on", True),
            ("1", True),
            (1, True),
            (True, True),
            ("false", False),
            ("off", False),
            ("0", False),
            (0, False),
            (False, False),
        ],
    )
    async def test_boolean_parameters_arrive_as_bool(self, feature, sent, expected):
        out = await _dyn_tool(feature, "toggle").execute(enabled=sent)
        assert out["data"]["enabled"] is expected
        assert out["data"]["type"] == "bool"

    @pytest.mark.asyncio
    async def test_strings_objects_and_arrays_pass_through_unchanged(self, feature):
        """A numeric-looking string stays a string; structures are untouched."""
        out = await _dyn_tool(feature, "structured").execute(
            label="30",
            tags=["a", "1"],
            options={"depth": "2"},
        )
        assert out["data"]["label"] == "30"
        assert out["data"]["tags"] == ["a", "1"]
        assert out["data"]["options"] == {"depth": "2"}

    @pytest.mark.asyncio
    async def test_optional_integer_still_coerces_when_supplied(self, feature):
        out = await _dyn_tool(feature, "optional_limit").execute(limit="5")
        assert out["data"]["limit"] == 5
        assert out["data"]["type"] == "int"


class TestJsonNull:
    """``null`` is a value for a nullable parameter and a type error otherwise.

    A model that sends JSON ``null`` for a non-nullable ``int`` reproduces
    issue #78 one line further in: ``min(None, 100)`` raises the same
    ``TypeError`` that ``min("30", 100)`` did, so the wrapper has to judge
    ``None`` by the declared type rather than wave it through.
    """

    @pytest.mark.asyncio
    async def test_json_null_for_an_optional_integer_passes_through(self, feature):
        out = await _dyn_tool(feature, "optional_limit").execute(limit=None)
        assert out["status"] == "ok"
        assert out["data"]["limit"] is None
        assert out["data"]["type"] == "NoneType"

    @pytest.mark.asyncio
    async def test_implicit_optional_none_default_also_accepts_null(self, feature):
        """``limit: int = None`` admits None as plainly as ``Optional[int]``."""
        out = await _dyn_tool(feature, "implicit_optional_limit").execute(limit=None)
        assert out["status"] == "ok"
        assert out["data"]["limit"] is None

    @pytest.mark.asyncio
    async def test_required_integer_rejects_null(self, feature):
        out = await _dyn_tool(feature, "required_count").execute(count=None)
        assert out["status"] == "error"
        assert "count" in out["error"]
        assert "integer" in out["error"]
        assert out["tool"] == "required_count"

    @pytest.mark.asyncio
    async def test_the_min_none_door_never_reaches_the_method(self, feature):
        """The promised failed ToolResult, not the legacy exception envelope."""
        out = await _dyn_tool(feature, "required_count").execute(count=None)
        assert feature.calls == []
        assert ToolResult.failed(out["error"]).status.value == out["status"]
        assert "not supported between instances" not in str(out)

    @pytest.mark.asyncio
    async def test_integer_too_large_for_a_float_is_a_coercion_failure(self, feature):
        """``float(10**309)`` raises OverflowError; the wrapper must not leak it.

        Coercion runs before the method call and outside the method's own
        exception handling, so an uncaught error here escapes ``execute``
        entirely instead of producing the promised failed ToolResult.
        """
        out = await _dyn_tool(feature, "scale").execute(factor=10**309)
        assert out["status"] == "error"
        assert "factor" in out["error"]
        assert "number" in out["error"]
        assert feature.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sent", ["NaN", "nan", "Infinity", "-inf", "1e309", float("inf"), float("nan")]
    )
    async def test_non_finite_numbers_are_a_coercion_failure(self, feature, sent):
        """JSON has no NaN or Infinity; ``float`` accepts them, the tool must not."""
        out = await _dyn_tool(feature, "scale").execute(factor=sent)
        assert out["status"] == "error"
        assert "factor" in out["error"]
        assert feature.calls == []

    @pytest.mark.asyncio
    async def test_non_none_default_does_not_make_a_parameter_nullable(self, feature):
        """``max_results: int = 30`` never contemplated None."""
        out = await _dyn_tool(feature, "get_comments").execute(
            issue="#78", max_results=None
        )
        assert out["status"] == "error"
        assert "max_results" in out["error"]
        assert feature.calls == []

    @pytest.mark.asyncio
    async def test_required_number_and_boolean_reject_null(self, feature):
        scaled = await _dyn_tool(feature, "scale").execute(factor=None)
        assert scaled["status"] == "error"
        assert "factor" in scaled["error"]

        toggled = await _dyn_tool(feature, "toggle").execute(enabled=None)
        assert toggled["status"] == "error"
        assert "enabled" in toggled["error"]

    @pytest.mark.asyncio
    async def test_null_passes_through_for_string_array_and_object(self, feature):
        """Only the coerced types judge None; structured values are untouched."""
        out = await _dyn_tool(feature, "structured").execute(
            label=None, tags=None, options=None
        )
        assert out["status"] == "ok"
        assert out["data"] == {"label": None, "tags": None, "options": None}

    @pytest.mark.parametrize(
        "name,param,expected",
        [
            ("get_comments", "max_results", False),
            ("required_count", "count", False),
            ("toggle", "enabled", False),
            ("optional_limit", "limit", True),
            ("implicit_optional_limit", "limit", True),
        ],
    )
    def test_schema_records_nullability_from_the_signature(
        self, feature, name, param, expected
    ):
        """The decorator has to keep what ``_resolve_json_type`` worked out;
        discarding it is what let a null through in the first place."""
        params = _dyn_tool(feature, name).schema.parameters
        declared = {p.name: p for p in params}[param]
        assert declared.nullable is expected


class TestUncoercibleArguments:
    """A value that cannot be coerced fails before the feature runs."""

    @pytest.mark.asyncio
    async def test_failure_names_the_parameter_and_the_expected_type(self, feature):
        out = await _dyn_tool(feature, "get_comments").execute(
            issue="#78", max_results="thirty"
        )
        assert out["status"] == "error"
        assert "max_results" in out["error"]
        assert "integer" in out["error"]
        assert "'thirty'" in out["error"]
        assert out["tool"] == "get_comments"
        assert "confirmation" not in out

    @pytest.mark.asyncio
    async def test_the_feature_method_is_never_called(self, feature):
        await _dyn_tool(feature, "get_comments").execute(
            issue="#78", max_results="thirty"
        )
        assert feature.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sent", ["thirty", "1.5", "", True, [30], {"n": 30}])
    async def test_integer_parameters_reject_uncoercible_values(self, feature, sent):
        out = await _dyn_tool(feature, "get_comments").execute(
            issue="#78", max_results=sent
        )
        assert out["status"] == "error"

    @pytest.mark.asyncio
    async def test_boolean_parameters_reject_an_arbitrary_word(self, feature):
        out = await _dyn_tool(feature, "toggle").execute(enabled="maybe")
        assert out["status"] == "error"
        assert "enabled" in out["error"]
        assert "boolean" in out["error"]

    @pytest.mark.asyncio
    async def test_number_parameters_reject_an_arbitrary_word(self, feature):
        out = await _dyn_tool(feature, "scale").execute(factor="half")
        assert out["status"] == "error"
        assert "factor" in out["error"]
        assert "number" in out["error"]

    @pytest.mark.asyncio
    async def test_rejected_value_is_bounded_in_the_error(self, feature):
        """The error re-enters the model's context, so a huge rejected value
        is summarized rather than echoed back whole."""
        out = await _dyn_tool(feature, "get_comments").execute(
            issue="#78", max_results="x" * 5000
        )
        assert out["status"] == "error"
        assert len(out["error"]) < 400
        assert "max_results" in out["error"]
        assert "integer" in out["error"]

    @pytest.mark.asyncio
    async def test_failure_envelope_is_a_toolresult_not_a_raised_typeerror(
        self, feature
    ):
        """The honesty layer reads ``status`` at the top level, so the
        rejection has to arrive as the canonical envelope."""
        out = await _dyn_tool(feature, "get_comments").execute(
            issue="#78", max_results="thirty"
        )
        rebuilt = ToolResult.failed(out["error"])
        assert rebuilt.status.value == out["status"]


class TestCommandPrefixPath:
    """The ``!tool arg`` text path keeps working and agrees with the JSON one."""

    def test_command_parsing_still_coerces_positionally(self, feature):
        args = _dyn_tool(feature, "get_comments").parse_command_args(
            "!get-comments #78 30"
        )
        assert args == {"issue": "#78", "max_results": 30}

    @pytest.mark.asyncio
    async def test_parsed_command_args_execute_unchanged(self, feature):
        dyn = _dyn_tool(feature, "get_comments")
        out = await dyn.execute(**dyn.parse_command_args("!get-comments #78 30"))
        assert out["status"] == "ok"
        assert out["data"]["per_page"] == 30

    @pytest.mark.asyncio
    async def test_uncoercible_command_token_is_rejected_by_the_same_door(
        self, feature
    ):
        """``parse_command_args`` keeps its lenient string fallback; the
        wrapper is what turns that into a named failure."""
        dyn = _dyn_tool(feature, "get_comments")
        args = dyn.parse_command_args("!get-comments #78 thirty")
        assert args["max_results"] == "thirty"
        out = await dyn.execute(**args)
        assert out["status"] == "error"
        assert "max_results" in out["error"]


class _ParityTool(AgentTool):
    """Minimal concrete tool, only to reach ``AgentTool._coerce_type``."""

    @property
    def name(self) -> str:
        return "parity"

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="parity",
            description="parity fixture",
            category=ToolCategory.UTILITY,
        )

    async def execute(self, **kwargs) -> Dict[str, Any]:
        return {"success": True, "result": kwargs, "tool": self.name}


class TestSharedRules:
    """Both paths run the same rules, so they cannot drift apart."""

    @pytest.mark.parametrize(
        "value,param_type",
        [
            ("30", "integer"),
            ("-4", "integer"),
            ("1.5", "number"),
            ("true", "boolean"),
            ("OFF", "boolean"),
            ("hello", "string"),
        ],
    )
    def test_command_path_matches_the_json_rules(self, value, param_type):
        assert _ParityTool()._coerce_type(value, param_type) == coerce_json_value(
            value, param_type
        )

    @pytest.mark.parametrize(
        "value,param_type",
        [("thirty", "integer"), ("half", "number"), ("maybe", "boolean")],
    )
    def test_command_path_keeps_its_lenient_string_fallback(self, value, param_type):
        """An uncoercible token is a hard failure for JSON arguments but is
        still handed back verbatim to the text parser."""
        assert coerce_json_value(value, param_type) is COERCION_FAILED
        assert _ParityTool()._coerce_type(value, param_type) == value

    @pytest.mark.parametrize(
        "value,param_type,expected",
        [
            (2.0, "integer", 2),
            (7, "integer", 7),
            ({"a": 1}, "object", {"a": 1}),
            ([1, 2], "array", [1, 2]),
            (30, "string", 30),
        ],
    )
    def test_json_rules_for_values_the_text_parser_never_sees(
        self, value, param_type, expected
    ):
        assert coerce_json_value(value, param_type) == expected

    @pytest.mark.parametrize("param_type", ["integer", "number", "boolean"])
    def test_none_is_governed_by_the_nullable_flag(self, param_type):
        assert coerce_json_value(None, param_type, nullable=True) is None
        assert coerce_json_value(None, param_type, nullable=False) is COERCION_FAILED

    @pytest.mark.parametrize("param_type", ["string", "object", "array"])
    def test_none_passes_through_for_uncoerced_types(self, param_type):
        """These types are handed over untouched, so there is nothing to fail."""
        assert coerce_json_value(None, param_type) is None
        assert coerce_json_value(None, param_type, nullable=True) is None

    def test_strict_is_the_default_so_a_forgetful_caller_fails_closed(self):
        """The default matches ``ToolParameter.nullable``'s own default."""
        assert coerce_json_value(None, "integer") is COERCION_FAILED
