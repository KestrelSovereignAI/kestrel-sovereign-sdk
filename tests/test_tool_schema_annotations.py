"""@tool parameter-schema generation from real and PEP 563 annotations.

Regression for review finding F003: the decorator mapped annotations by object
identity against a small type_map, so ``Optional[X]``, PEP 604 unions, and —
critically — EVERY parameter in a module using ``from __future__ import
annotations`` (where annotations are strings) silently degraded to type
``"string"``. That produced contradictory LLM-facing schemas (e.g. an int param
advertised as a string with an int default) and forced defensive coercion
spackle at call sites.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from kestrel_sdk.features.base import _resolve_json_type, tool


def _params(method) -> dict[str, str]:
    return {p.name: p.type for p in method._tool_schema["parameters"]}


def test_resolve_optional_and_union_and_generics():
    assert _resolve_json_type(Optional[int])[0] == "integer"
    assert _resolve_json_type(int | None)[0] == "integer"
    assert _resolve_json_type(Optional[int])[2] is True  # nullable
    json_type, items, _, _ = _resolve_json_type(Optional[List[str]])
    assert json_type == "array"
    assert items == {"type": "string"}
    assert _resolve_json_type(bool)[0] == "boolean"
    assert _resolve_json_type(dict)[0] == "object"


def test_resolved_flag_distinguishes_mapping_from_fallback():
    # Deliberate mappings — resolved is True (no warning), including the
    # str / Optional[str] cases whose JSON type legitimately IS "string".
    assert _resolve_json_type(str)[3] is True
    assert _resolve_json_type(Optional[str])[3] is True
    assert _resolve_json_type(str | None)[3] is True
    assert _resolve_json_type(int)[3] is True
    assert _resolve_json_type(Optional[List[str]])[3] is True

    # Genuine fallbacks — resolved is False (warning fires).
    assert _resolve_json_type(int | str)[3] is False  # ambiguous multi-type union
    assert _resolve_json_type(Optional[Callable[[int], None]])[3] is False


def test_no_warning_for_optional_str_but_warning_for_true_fallback(caplog):
    with caplog.at_level(logging.WARNING, logger="kestrel_sdk.features.base"):

        @tool("warn_demo", "warn demo")
        async def warn_demo(self, note: Optional[str] = None, timeout: int | str = 30):
            """Demo.

            Args:
                note: an optional note
                timeout: seconds or literal
            """

    messages = [r.getMessage() for r in caplog.records]
    # Optional[str] maps cleanly — must NOT warn.
    assert not any("'note'" in m for m in messages)
    # int | str is a genuine fallback — must warn.
    assert any("'timeout'" in m for m in messages)


def test_tool_schema_under_pep563_string_annotations():
    # This whole module has `from __future__ import annotations`, so the
    # annotations below are strings at decoration time — the exact condition
    # that used to degrade every param to "string".
    @tool("demo", "demo tool")
    async def demo(self, max_count: int = 20, tags: Optional[List[str]] = None, flag: bool = False):
        """Demo.

        Args:
            max_count: how many
            tags: optional tags
            flag: a flag
        """

    params = _params(demo)
    assert params["max_count"] == "integer"
    assert params["tags"] == "array"
    assert params["flag"] == "boolean"


def test_unresolvable_annotation_falls_back_to_string_without_crashing():
    # A forward reference to a name that cannot be resolved must degrade
    # gracefully (fall back to string), not raise at decoration time.
    @tool("demo2", "demo tool 2")
    async def demo2(self, thing: "NotARealTypeName" = None):  # noqa: F821
        """Demo.

        Args:
            thing: a thing
        """

    assert _params(demo2)["thing"] == "string"


def test_raw_str_annotation_after_hint_failure_resolves_without_warning(caplog):
    # When one param is an unresolvable forward ref, get_type_hints() fails for
    # the whole function and ALL annotations degrade to raw strings. A valid
    # `name: str` then arrives as the literal "str" — it must still resolve and
    # must NOT emit the fallback warning (only the genuinely broken ref does).
    assert _resolve_json_type("str")[0] == "string"
    assert _resolve_json_type("str")[3] is True

    with caplog.at_level(logging.WARNING, logger="kestrel_sdk.features.base"):

        @tool("demo3", "demo tool 3")
        async def demo3(self, name: str = "x", bad: "NotARealTypeName" = None):  # noqa: F821
            """Demo.

            Args:
                name: a name
                bad: unresolvable
            """

    params = _params(demo3)
    assert params["name"] == "string"
    assert params["bad"] == "string"
    messages = [r.getMessage() for r in caplog.records]
    assert not any("'name'" in m for m in messages)  # clean str — no warning
    assert any("'bad'" in m for m in messages)  # unresolvable — warns
