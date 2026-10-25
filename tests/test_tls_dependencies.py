"""Public install contracts for the SDK's optional HTTPS clients."""

from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import pytest


def selected_requirements(extra):
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    pending, seen, requirements = [extra], set(), []
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for raw in project["optional-dependencies"][name]:
            requirement = Requirement(raw)
            if canonicalize_name(requirement.name) == canonicalize_name(project["name"]):
                pending.extend(requirement.extras)
            else:
                requirements.append(requirement)
    return requirements


@pytest.mark.parametrize("extra", ["a2a", "httpx", "httpx2", "tracing"])
def test_every_https_profile_requires_the_current_post_incident_trust_bundle(extra):
    roots = [item for item in selected_requirements(extra)
             if canonicalize_name(item.name) == "certifi"]
    assert roots, "Every HTTPS profile must declare its trust-bundle policy"
    assert all(not item.specifier.contains("2026.5.20") for item in roots)
    assert all(item.specifier.contains("2026.10.23") for item in roots)
    assert all(item.specifier.contains("2099.1.1") for item in roots), "Keep future CA updates eligible"


def test_the_base_sdk_does_not_acquire_an_http_trust_bundle_dependency():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    assert {canonicalize_name(Requirement(item).name) for item in project["dependencies"]} == {"pydantic", "idna"}
