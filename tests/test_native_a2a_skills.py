"""Features contribute native skills; only the serving agent owns a card."""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest
from google.protobuf.json_format import MessageToDict

from kestrel_sdk.native_a2a.types import AgentSkill, InvalidParamsError
from kestrel_sdk.features.base import Feature, tool
from kestrel_sdk.tools.base import ToolCategory


class SkillFeature(Feature):
    @property
    def tool_description(self) -> str:
        return "Skill contribution fixture"

    async def initialize(self) -> None:
        return None

    @tool(name="save_note", description="Save a note", category=ToolCategory.UTILITY)
    async def save_note(self, note: str) -> dict:
        return {"note": note}

    @tool(name="read_note", description="Read a note", category=ToolCategory.UTILITY)
    async def read_note(self) -> dict:
        return {"note": "saved"}


def test_skills_are_official_models_with_native_json() -> None:
    skills = SkillFeature(agent=None).get_agent_skills()
    assert all(isinstance(skill, AgentSkill) for skill in skills)
    assert [MessageToDict(skill) for skill in skills] == [
        {
            "id": name,
            "name": name,
            "description": description,
            "tags": ["utility"],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        }
        for name, description in (
            ("read_note", "Read a note"), ("save_note", "Save a note")
        )
    ]


def test_skills_follow_current_tool_exposure_and_are_fresh() -> None:
    feature = SkillFeature(agent=None)
    first = feature.get_agent_skills()
    first[0].description = "caller mutation"
    feature.disabled_skills = {"save_note"}
    assert [MessageToDict(skill) for skill in feature.get_agent_skills()] == [
        {
            "id": "read_note", "name": "read_note", "description": "Read a note",
            "tags": ["utility"], "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        }
    ]


def test_skill_metadata_comes_from_current_tool_schema() -> None:
    feature = SkillFeature(agent=None)
    exposed = feature.get_tools()
    exposed[0]._schema_data = dict(exposed[0]._schema_data, description="Current schema")
    exposed[0].agent_skill = object()
    feature.get_tools = lambda: exposed
    assert feature.get_agent_skills()[0].description == "Current schema"


def test_empty_exposure_has_no_skills() -> None:
    feature = SkillFeature(agent=None)
    feature.disabled_skills = {"save_note", "read_note"}
    assert feature.get_agent_skills() == []


@pytest.mark.parametrize("name,description", [("", "Valid description"), ("valid", "")])
def test_invalid_required_skill_fields_fail_with_the_native_error(name: str, description: str) -> None:
    class InvalidFeature(SkillFeature):
        @tool(name=name, description=description)
        async def invalid(self) -> dict:
            return {}

    with pytest.raises(InvalidParamsError) as error:
        InvalidFeature(agent=None).get_agent_skills()
    expected = "description" if not description else "id"
    assert expected in {item["field"] for item in error.value.data["errors"]}


def test_features_do_not_publish_fake_agent_cards() -> None:
    assert not hasattr(SkillFeature(agent=None), "get_agent_card")


@pytest.mark.parametrize("module", ["kestrel_sdk.a2a.types", "kestrel_sdk.a2a.agent_card"])
def test_draft_wire_models_are_retired(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_decorator_does_not_duplicate_protocol_metadata() -> None:
    assert not hasattr(SkillFeature.save_note, "_agent_skill")
    assert all(not hasattr(item, "agent_skill") for item in SkillFeature(agent=None).get_tools())


def test_feature_import_and_tool_execution_need_no_a2a_extra() -> None:
    code = '''
import asyncio
import importlib.abc
import sys

class BlockA2A(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "google.protobuf" or fullname.startswith("google.protobuf."):
            raise ModuleNotFoundError("blocked optional protobuf", name="google.protobuf")
        if fullname == "a2a" or fullname.startswith("a2a."):
            raise AssertionError("external A2A SDK must not be imported")
sys.meta_path.insert(0, BlockA2A())
from kestrel_sdk.features.base import Feature, tool

class Example(Feature):
    tool_description = "Example"
    async def initialize(self):
        return None
    @tool(name="echo", description="Echo")
    async def echo(self, text: str):
        return {"text": text}

feature = Example(agent=None)
assert asyncio.run(feature.get_tools()[0].execute(text="hello"))["result"] == {"text": "hello"}
assert not any(name == "a2a" or name.startswith("a2a.") for name in sys.modules)
assert not any(name.startswith("kestrel_sdk.native_a2a") for name in sys.modules)
try:
    feature.get_agent_skills()
except ModuleNotFoundError as error:
    assert error.name == "google.protobuf"
else:
    raise AssertionError("native skill contribution requires the optional a2a extra")
'''
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
