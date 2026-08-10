"""Kestrel SDK — Feature interfaces."""

from importlib import import_module
from typing import TYPE_CHECKING

from .base import Feature, tool, parse_docstring_params, TaskHandler
from .host_base import HostContext, HostFeature
from .ui import UIContributions

_CONTRIBUTION_EXPORTS = frozenset(
    {
        "ContributionContractError",
        "ContributionResult",
        "FeatureContributionSet",
        "FeaturePermissionDefaults",
        "PermissionLevel",
        "ServiceContributions",
        "SetupFlow",
        "SetupStep",
        "SetupStepClassification",
        "SetupStepContext",
        "SetupStepContributions",
        "SetupStepRegistration",
        "WaitProviderContributions",
        "WaitProviderRegistration",
        "WorkflowActor",
        "WorkflowContributions",
        "WorkflowRegistration",
        "await_contribution_result",
        "normalize_setup_flow",
        "order_setup_step_registrations",
        "validate_contribution_owner_uniqueness",
        "validate_feature_contributions",
    }
)

if TYPE_CHECKING:  # pragma: no cover - static analysis only
    from .contributions import (
        ContributionContractError,
        ContributionResult,
        FeatureContributionSet,
        FeaturePermissionDefaults,
        PermissionLevel,
        ServiceContributions,
        SetupFlow,
        SetupStep,
        SetupStepClassification,
        SetupStepContext,
        SetupStepContributions,
        SetupStepRegistration,
        WaitProviderContributions,
        WaitProviderRegistration,
        WorkflowActor,
        WorkflowContributions,
        WorkflowRegistration,
        await_contribution_result,
        normalize_setup_flow,
        order_setup_step_registrations,
        validate_contribution_owner_uniqueness,
        validate_feature_contributions,
    )


def __getattr__(name: str) -> object:
    """Load the optional contribution surface only when it is requested."""

    if name not in _CONTRIBUTION_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".contributions", __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "Feature",
    "tool",
    "parse_docstring_params",
    "TaskHandler",
    "HostFeature",
    "HostContext",
    "UIContributions",
    "ContributionContractError",
    "ContributionResult",
    "FeatureContributionSet",
    "FeaturePermissionDefaults",
    "PermissionLevel",
    "ServiceContributions",
    "SetupFlow",
    "SetupStep",
    "SetupStepClassification",
    "SetupStepContext",
    "SetupStepContributions",
    "SetupStepRegistration",
    "WaitProviderContributions",
    "WaitProviderRegistration",
    "WorkflowActor",
    "WorkflowContributions",
    "WorkflowRegistration",
    "await_contribution_result",
    "normalize_setup_flow",
    "order_setup_step_registrations",
    "validate_contribution_owner_uniqueness",
    "validate_feature_contributions",
]
