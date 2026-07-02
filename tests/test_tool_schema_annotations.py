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

from typing import List, Optional

from kestrel_sdk.features.base import _resolve_json_type, tool


def _params(method) -> dict[str, str]:
    return {p.name: p.type for p in method._tool_schema["parameters"]}


def test_resolve_optional_and_union_and_generics():
    assert _resolve_json_type(Optional[int])[0] == "integer"
    assert _resolve_json_type(int | None)[0] == "integer"
    assert _resolve_json_type(Optional[int])[2] is True  # nullable
    json_type, items, _ = _resolve_json_type(Optional[List[str]])
    assert json_type == "array"
    assert items == {"type": "string"}
    assert _resolve_json_type(bool)[0] == "boolean"
    assert _resolve_json_type(dict)[0] == "object"


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
