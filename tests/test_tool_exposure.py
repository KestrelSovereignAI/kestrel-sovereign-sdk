"""Tool model exposure is host metadata, separate from schema and permission."""

import pytest

from kestrel_sdk.tools import ToolCategory, ToolParameter, ToolSchema


def schema(**options):
    return ToolSchema(
        name="connection_status",
        description="Read connection status",
        category=ToolCategory.SYSTEM,
        parameters=[ToolParameter(name="connection", type="string", description="Connection")],
        **options,
    )


def test_tools_are_visible_to_the_model_by_default():
    assert schema().model_visible is True


@pytest.mark.parametrize("visible", [True, False])
def test_tool_descriptor_retains_explicit_model_exposure(visible):
    tool = schema(model_visible=visible)
    assert tool.model_visible is visible
    assert tool.to_dict()["model_visible"] is visible


def test_model_exposure_does_not_change_the_native_provider_schema():
    visible = schema(model_visible=True)
    hidden = schema(model_visible=False)
    assert hidden.to_openai_format() == visible.to_openai_format()
    assert set(hidden.to_openai_format()) == {"type", "function"}
    assert set(hidden.to_openai_format()["function"]) == {"name", "description", "parameters"}


@pytest.mark.parametrize("invalid", [None, 0, 1, "false", []])
def test_model_exposure_requires_an_explicit_boolean(invalid):
    with pytest.raises(TypeError, match="model_visible must be a boolean"):
        schema(model_visible=invalid)
