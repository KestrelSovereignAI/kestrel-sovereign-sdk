"""Contract tests shared by agent UI and application extensions."""

from kestrel_sdk import AppExtension, UIContributions
from kestrel_sdk.features.base import Feature


class _BareFeature(Feature):
    @property
    def tool_description(self):
        return "bare"

    async def initialize(self):
        pass


def test_agent_feature_ui_contract_is_sdk_owned():
    feature = _BareFeature(agent=object())

    assert feature.get_ui_contributions() is None
    contribution = UIContributions(
        modules=["panel.js"],
        css=["panel.css"],
        static_dir="/pkg/static",
        capability="example",
    )
    assert contribution.css == ["panel.css"]


def test_ui_contributions_preserves_0292_positional_order():
    contribution = UIContributions("/pkg/static", ["panel.js"], "example")

    assert contribution.static_dir == "/pkg/static"
    assert contribution.modules == ["panel.js"]
    assert contribution.capability == "example"
    assert contribution.css == []


def test_app_extension_defaults_are_safe_noops():
    agent = object()
    extension = AppExtension(agent)

    assert extension.agent is agent
    assert extension._agent is agent
    assert extension.pre_process_input("hello") is None
    assert extension.post_process_response("reply", {"model": "test"}) == "reply"
    assert extension.get_system_prompt_prefix() == ""
    assert extension.get_constitution_amendments() == ""
