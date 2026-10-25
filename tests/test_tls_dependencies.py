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
            if requirement.marker is not None and not requirement.marker.evaluate({"extra": name}):
                continue
            if canonicalize_name(requirement.name) == canonicalize_name(project["name"]):
                pending.extend(requirement.extras)
            else:
                requirements.append(requirement)
    return requirements


@pytest.mark.parametrize("extra", ["a2a", "httpx", "httpx2", "tracing"])
def test_https_profiles_share_the_certifi_dependency_policy(extra):
    roots = [item for item in selected_requirements(extra)
             if canonicalize_name(item.name) == "certifi"]
    assert roots, "HTTPS profiles must carry the shared certifi dependency policy"
    assert all(not item.specifier.contains("2026.5.20") for item in roots)
    assert all(not item.specifier.contains("2026.10.18") for item in roots)
    assert all(not item.specifier.contains("2026.10.22") for item in roots)
    assert all(item.specifier.contains("2026.10.23") for item in roots)
    assert all(item.specifier.contains("2099.1.1") for item in roots), "Keep future CA updates eligible"
