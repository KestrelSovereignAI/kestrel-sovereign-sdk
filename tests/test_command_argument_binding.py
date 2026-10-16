"""``!command`` argument binding: quoting, ``name=value`` and named failures (#73).

``parse_command_args`` used to split on bare whitespace and let only the final
parameter take the remaining words. A tool whose first parameter is free text
broke as soon as the user typed two words: the extra words spilled into the
next parameter, and the user read ``Unknown kind: window`` about a parameter
they never typed. The docstring promised quoted strings the code never
handled, and a non-final JSON ``args_json`` containing spaces was shredded
across the parameters after it (kestrel-sovereign#3118).

Every test here types multi-word or quoted input. The old suite only ever
passed single words, which is the one assumption it shared with the bug.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from kestrel_sdk.features.base import Feature, tool
from kestrel_sdk.tools import AgentTool, CommandParseError, ToolCategory
from kestrel_sdk.tools.result import ToolResult

_KINDS = ("all", "patterns", "blockers")
_INTENT = {"intent": "check whether CI on PR 3096 went green, then merge"}
_INTENT_JSON = json.dumps(_INTENT)


class _CommandFeature(Feature):
    """Tools shaped like the command tools from the two bug reports."""

    def __init__(self, agent=None):
        super().__init__(agent)
        self.calls: List[Dict[str, Any]] = []

    @property
    def tool_description(self) -> str:  # noqa: D401  (single-line)
        return "fixture feature for command-binding tests"

    async def initialize(self) -> None:
        """No-op; the fixture doesn't need real lifecycle setup."""
        return None

    @tool(
        name="strategy_search",
        description="Free text first, then a value the feature validates",
        category=ToolCategory.MEMORY,
        command_prefix="!strategy-search",
    )
    async def strategy_search(self, query: str, kind: str = "all") -> ToolResult:
        """
        Search the strategy ledger.

        Args:
            query: Words to match against the ledger rows
            kind: Which rows to search -- all, patterns, blockers
        """
        if kind not in _KINDS:
            return ToolResult.failed(f"Unknown kind: {kind}")
        self.calls.append({"query": query, "kind": kind})
        return ToolResult.ok("searched", data={"query": query, "kind": kind})

    @tool(
        name="ledger_search",
        description="strategy_search's full production signature",
        category=ToolCategory.MEMORY,
        command_prefix="!ledger-search",
    )
    async def ledger_search(
        self,
        query: str,
        kind: str = "all",
        limit: int = 10,
        include_retired: bool = False,
    ) -> ToolResult:
        """
        Search the strategy ledger with every production option.

        Args:
            query: Words to match against the ledger rows
            kind: Which rows to search -- all, patterns, blockers
            limit: Maximum matches to return
            include_retired: Include superseded and resolved rows
        """
        if kind not in _KINDS:
            return ToolResult.failed(f"Unknown kind: {kind}")
        self.calls.append({"query": query, "kind": kind, "limit": limit})
        return ToolResult.ok("searched")

    @tool(
        name="schedule_add_deadline",
        description="A non-final string parameter that carries JSON",
        category=ToolCategory.UTILITY,
        command_prefix="!schedule deadline",
    )
    async def schedule_add_deadline(
        self,
        run_at: str = "",
        task_name: str = "",
        args_json: str = "{}",
        misfire_policy: str = "fire_once",
        misfire_grace_seconds: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        delay_seconds: Optional[int] = None,
    ) -> ToolResult:
        """
        Add a one-shot deadline.

        Args:
            run_at: ISO-8601 deadline
            task_name: Task to run
            args_json: JSON arguments for the task
            misfire_policy: What to do with a missed deadline
            misfire_grace_seconds: Grace period for a missed deadline
            idempotency_key: Deduplication key
            delay_seconds: Relative delay instead of run_at
        """
        self.calls.append({
            "run_at": run_at,
            "task_name": task_name,
            "args": json.loads(args_json),
            "misfire_policy": misfire_policy,
            "misfire_grace_seconds": misfire_grace_seconds,
            "idempotency_key": idempotency_key,
            "delay_seconds": delay_seconds,
        })
        return ToolResult.ok("scheduled")

    @tool(
        name="typed",
        description="One parameter of every JSON type",
        category=ToolCategory.UTILITY,
        command_prefix="!typed",
    )
    async def typed(
        self,
        label: str = "",
        count: int = 0,
        ratio: float = 1.0,
        enabled: bool = False,
        options: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
    ) -> ToolResult:
        """
        Accept one value of each type.

        Args:
            label: A string
            count: An integer
            ratio: A number
            enabled: A boolean
            options: An object
            tags: An array
        """
        self.calls.append({"label": label, "count": count})
        return ToolResult.ok("typed")

    @tool(
        name="memory_search",
        description="A multi-word command prefix",
        category=ToolCategory.MEMORY,
        command_prefix="!memory search",
    )
    async def memory_search(self, query: str, limit: int = 20) -> ToolResult:
        """
        Search memory.

        Args:
            query: Search query
            limit: Maximum results
        """
        return ToolResult.ok("found")

    @tool(
        name="status",
        description="Takes no arguments",
        category=ToolCategory.SYSTEM,
        command_prefix="!status",
    )
    async def status(self) -> ToolResult:
        """Report status."""
        return ToolResult.ok("fine")


@pytest.fixture
def feature() -> _CommandFeature:
    return _CommandFeature()


def _tool(feature: Feature, name: str) -> AgentTool:
    for candidate in feature.get_tools():
        if candidate.name == name:
            return candidate
    raise AssertionError(f"tool {name!r} not found")


def _parse(feature: Feature, name: str, line: str) -> Dict[str, Any]:
    return _tool(feature, name).parse_command_args(line)


def _parse_error(feature: Feature, name: str, line: str) -> CommandParseError:
    with pytest.raises(CommandParseError) as caught:
        _parse(feature, name, line)
    return caught.value


class TestFreeTextFirstParameter:
    """The ``strategy_search`` report: free text that is not the last parameter.

    The ``strategy_search`` fixture keeps only the two parameters the defect
    needs: the old parser gave ``kind`` the trailing words and the feature
    answered ``Unknown kind``. ``ledger_search`` has the full production
    signature.
    """

    @pytest.mark.parametrize(
        "line",
        [
            '!strategy-search "context window bloat"',
            "!strategy-search 'context window bloat'",
        ],
    )
    def test_quoted_query_binds_whole(self, feature, line):
        assert _parse(feature, "strategy_search", line) == {
            "query": "context window bloat",
            "kind": "all",
        }

    @pytest.mark.asyncio
    async def test_quoted_query_reaches_the_method_whole(self, feature):
        dyn = _tool(feature, "strategy_search")
        out = await dyn.execute(
            **dyn.parse_command_args('!strategy-search "context window bloat"')
        )
        assert out["status"] == "ok"
        assert feature.calls == [{"query": "context window bloat", "kind": "all"}]

    def test_unquoted_words_are_too_many_arguments_not_unknown_kind(self, feature):
        error = _parse_error(
            feature, "strategy_search", "!strategy-search context window bloat"
        )
        message = str(error)
        assert message.startswith("!strategy-search: too many arguments")
        assert "'context', 'window', 'bloat'" in message
        assert "quote multi-word values or use name=value" in message
        assert "Unknown kind" not in message
        assert feature.calls == []

    def test_named_query_takes_a_quoted_value(self, feature):
        line = '!strategy-search query="context window bloat" kind=blockers'
        assert _parse(feature, "strategy_search", line) == {
            "query": "context window bloat",
            "kind": "blockers",
        }

    def test_quoted_query_then_typed_positionals_on_the_full_signature(self, feature):
        line = '!ledger-search "context window bloat" blockers 5 true'
        assert _parse(feature, "ledger_search", line) == {
            "query": "context window bloat",
            "kind": "blockers",
            "limit": 5,
            "include_retired": True,
        }

    def test_quoted_query_then_named_values_on_the_full_signature(self, feature):
        line = "!ledger-search 'context window bloat' limit=5 include_retired=yes"
        assert _parse(feature, "ledger_search", line) == {
            "query": "context window bloat",
            "kind": "all",
            "limit": 5,
            "include_retired": True,
        }

    def test_unquoted_words_beyond_the_full_signature_are_too_many(self, feature):
        error = _parse_error(
            feature, "ledger_search", "!ledger-search why is the context window bloated"
        )
        assert "too many arguments" in str(error)
        assert "at most 4 positional values" in str(error)


class TestJsonArgumentWithSpaces:
    """The kestrel-sovereign#3118 report: JSON in a non-final string parameter."""

    _LATER = ("misfire_grace_seconds", "idempotency_key", "delay_seconds")

    @pytest.mark.parametrize(
        "quoted",
        [
            f"'{_INTENT_JSON}'",
            '"' + _INTENT_JSON.replace('"', '\\"') + '"',
        ],
    )
    def test_quoted_positional_json_binds_exactly(self, feature, quoted):
        args = _parse(
            feature,
            "schedule_add_deadline",
            f"!schedule deadline 2026-08-26T12:00:00+00:00 self_followup {quoted}",
        )
        assert args == {
            "run_at": "2026-08-26T12:00:00+00:00",
            "task_name": "self_followup",
            "args_json": _INTENT_JSON,
            # Its declared default, not a fragment of the JSON.
            "misfire_policy": "fire_once",
        }
        assert not any(name in args for name in self._LATER)

    def test_named_json_binds_exactly_in_the_reported_form(self, feature):
        """The ``key=value`` line from #3118 that used to bind nothing."""
        args = _parse(
            feature,
            "schedule_add_deadline",
            f"!schedule deadline delay_seconds=1200 task_name=self_followup "
            f"args_json='{_INTENT_JSON}'",
        )
        assert args["args_json"] == _INTENT_JSON
        assert args["task_name"] == "self_followup"
        assert args["delay_seconds"] == 1200
        assert args["run_at"] == ""
        assert "misfire_grace_seconds" not in args
        assert "idempotency_key" not in args

    @pytest.mark.asyncio
    async def test_named_json_reaches_the_method_exactly(self, feature):
        dyn = _tool(feature, "schedule_add_deadline")
        args = dyn.parse_command_args(
            "!schedule deadline 2026-08-26T12:00:00+00:00 self_followup "
            f"args_json='{_INTENT_JSON}'"
        )
        out = await dyn.execute(**args)
        assert out["status"] == "ok"
        assert feature.calls == [{
            "run_at": "2026-08-26T12:00:00+00:00",
            "task_name": "self_followup",
            "args": _INTENT,
            "misfire_policy": "fire_once",
            "misfire_grace_seconds": None,
            "idempotency_key": None,
            "delay_seconds": None,
        }]

    def test_unquoted_json_with_spaces_fails_loudly(self, feature):
        """It used to bind its first fragment and spill the rest quietly."""
        error = _parse_error(
            feature,
            "schedule_add_deadline",
            "!schedule deadline 2026-08-26T12:00:00+00:00 self_followup "
            '{"intent": "check whether CI went green"}',
        )
        assert str(error).startswith("!schedule deadline: unexpected '}'")
        assert "quote the whole value" in str(error)
        assert feature.calls == []

    def test_whitespace_free_unquoted_json_still_arrives_verbatim(self, feature):
        args = _parse(
            feature,
            "schedule_add_deadline",
            '!schedule deadline @hourly github_pr_watch {"repo":"owner/self_followup"}',
        )
        assert args["args_json"] == '{"repo":"owner/self_followup"}'


class TestNameValueBinding:
    """``name=value`` binds by exact declared name, for every type."""

    @pytest.mark.parametrize(
        "assignment,name,expected",
        [
            ("count=30", "count", 30),
            ('count="30"', "count", 30),
            ("ratio=0.5", "ratio", 0.5),
            ("enabled=true", "enabled", True),
            ("enabled='off'", "enabled", False),
            ("options='{\"mode\": \"a b\", \"depth\": 2}'", "options", {"mode": "a b", "depth": 2}),
            ("tags='[\"x y\", \"z\"]'", "tags", ["x y", "z"]),
        ],
    )
    def test_each_type_after_a_quoted_positional(self, feature, assignment, name, expected):
        args = _parse(feature, "typed", f'!typed "release notes draft" {assignment}')
        assert args["label"] == "release notes draft"
        assert args[name] == expected
        assert type(args[name]) is type(expected)

    def test_string_takes_a_quoted_multi_word_value(self, feature):
        args = _parse(feature, "typed", "!typed label='release notes draft' count=3")
        assert args["label"] == "release notes draft"
        assert args["count"] == 3

    def test_every_type_in_one_line(self, feature):
        line = (
            "!typed label=\"release notes\" count=7 ratio=2.5 enabled=no "
            "options='{\"mode\": \"a b\"}' tags='[\"x y\"]'"
        )
        assert _parse(feature, "typed", line) == {
            "label": "release notes",
            "count": 7,
            "ratio": 2.5,
            "enabled": False,
            "options": {"mode": "a b"},
            "tags": ["x y"],
        }

    def test_nullable_object_accepts_json_null(self, feature):
        args = _parse(feature, "typed", "!typed 'release notes' options=null")
        assert args["options"] is None

    def test_unknown_key_is_a_positional_value(self, feature):
        """Free text containing ``a=b`` is not silently eaten as a name."""
        line = '!strategy-search scope="context window" kind=blockers'
        assert _parse(feature, "strategy_search", line) == {
            "query": "scope=context window",
            "kind": "blockers",
        }

    def test_key_must_match_the_declared_name_exactly(self, feature):
        line = "!strategy-search 'context window' Kind=blockers"
        assert _parse(feature, "strategy_search", line) == {
            "query": "context window",
            "kind": "Kind=blockers",
        }

    def test_a_quoted_declared_name_is_literal_text(self, feature):
        line = '!strategy-search "kind=blockers are urgent"'
        assert _parse(feature, "strategy_search", line) == {
            "query": "kind=blockers are urgent",
            "kind": "all",
        }

    def test_positional_then_named_for_the_same_parameter_is_an_error(self, feature):
        error = _parse_error(
            feature,
            "strategy_search",
            "!strategy-search 'context window' query='other words'",
        )
        assert "parameter 'query' is given twice" in str(error)
        assert "positionally as 'context window'" in str(error)

    def test_named_twice_is_an_error_naming_the_parameter(self, feature):
        error = _parse_error(feature, "typed", "!typed 'release notes' count=1 count=2")
        assert "parameter 'count' is given twice" in str(error)

    def test_positional_after_named_is_an_error(self, feature):
        error = _parse_error(
            feature, "strategy_search", "!strategy-search kind=blockers 'context window'"
        )
        assert "positional value 'context window' follows a name=value argument" in str(error)

    @pytest.mark.parametrize(
        "assignment,expected_type",
        [
            ("options='{\"mode\": a b}'", "object"),
            ("options='[\"x y\"]'", "object"),
            ("tags='{\"x\": \"y z\"}'", "array"),
        ],
    )
    def test_structured_value_must_be_json_of_its_shape(
        self, feature, assignment, expected_type
    ):
        name = assignment.split("=", 1)[0]
        error = _parse_error(feature, "typed", f"!typed 'release notes' {assignment}")
        assert f"parameter '{name}' expects a JSON {expected_type}" in str(error)

    @pytest.mark.asyncio
    async def test_uncoercible_named_integer_is_rejected_by_the_wrapper(self, feature):
        """Scalar types keep the lenient fallback so the ``@tool`` wrapper stays
        the one door that rejects them, naming the parameter."""
        dyn = _tool(feature, "typed")
        args = dyn.parse_command_args("!typed 'release notes' count=thirty")
        assert args["count"] == "thirty"
        out = await dyn.execute(**args)
        assert out["status"] == "error"
        assert "count" in out["error"]
        assert feature.calls == []


class TestQuoting:
    """Quotes delimit values where a value starts; elsewhere they are text."""

    @pytest.mark.parametrize(
        "line",
        [
            '!strategy-search "context window bloat',
            "!strategy-search 'tis the season",
            "!strategy-search 'context window' kind='blockers",
        ],
    )
    def test_unbalanced_quote_is_a_clear_parse_error(self, feature, line):
        error = _parse_error(feature, "strategy_search", line)
        assert isinstance(error, ValueError)
        assert error.command == "!strategy-search"
        assert str(error).startswith("!strategy-search: unbalanced quote")
        assert "is never closed" in str(error)

    def test_text_straight_after_a_closing_quote_is_an_error(self, feature):
        error = _parse_error(
            feature, "strategy_search", '!strategy-search "context window"bloat'
        )
        assert "unexpected 'b' after the closing \"" in str(error)

    def test_quotes_inside_an_unquoted_word_are_text(self, feature):
        line = "!strategy-search don't patterns"
        assert _parse(feature, "strategy_search", line) == {
            "query": "don't",
            "kind": "patterns",
        }

    def test_double_quotes_honour_backslash_escapes(self, feature):
        line = r'!strategy-search "say \"hi\" from C:\\temp"'
        assert _parse(feature, "strategy_search", line)["query"] == 'say "hi" from C:\\temp'

    def test_single_quotes_are_literal(self, feature):
        line = r"!strategy-search 'C:\temp \"raw\"'"
        assert _parse(feature, "strategy_search", line)["query"] == r'C:\temp \"raw\"'

    def test_empty_quoted_value_binds_an_empty_string(self, feature):
        line = '!strategy-search "" patterns'
        assert _parse(feature, "strategy_search", line) == {"query": "", "kind": "patterns"}

    def test_whitespace_inside_quotes_survives_a_multi_word_prefix(self, feature):
        line = '!memory  search   "blue   sky"  5'
        assert _parse(feature, "memory_search", line) == {"query": "blue   sky", "limit": 5}


class TestArgumentCount:
    """Words nothing can take are refused instead of dropped or spilled."""

    def test_tool_without_parameters_refuses_extra_words(self, feature):
        error = _parse_error(feature, "status", "!status right now")
        assert str(error) == (
            "!status: too many arguments: it takes no arguments but got 2 "
            "('right', 'now')"
        )

    def test_tool_without_parameters_still_parses_its_bare_command(self, feature):
        assert _parse(feature, "status", "!status") == {}

    @pytest.mark.parametrize("line", ["", "context window bloat", 'hello "world'])
    def test_non_command_input_returns_no_arguments(self, feature, line):
        assert _parse(feature, "strategy_search", line) == {}
