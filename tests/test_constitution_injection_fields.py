"""SDK Phase 1.1A — SourceRegistration constitutional-injection fields.

Pins the contract added in kestrel-sovereign#1137 (PR sovereign#1147,
design doc `docs/architecture/CONSTITUTION_INJECTION.md`):

* `require_constitution_echo: bool = False` — opt-in for the in-agent
  `claude_code` format; codex/local reviewer formats opt in via the
  registry validator (validator lives in sovereign, not the SDK).
* `prompt_template_format: Literal["claude_code","codex","local","bare"]
   = "claude_code"` — selects how the constitutional injection is wrapped
  for delivery to the actor.
* `constitution_injection: Literal["full","none"] = "none"` — tracks
  whether the source intends the dispatcher to inject the doctrine bundle
  for it. Default `"none"` matches current ARTIFACT reality.
* `system_prompt_budget_bytes: Optional[int] = None` — per-source override
  for the priority-ordered truncation budget; None means use the operator
  default.

The defaults must be backwards-compatible: an existing pre-0.11 source
registration constructed with only the previously-required fields must
still validate and round-trip cleanly through dataclass operations.
"""
from __future__ import annotations

from kestrel_sdk.signals import (
    RedactionPolicy,
    SignalMode,
    SourceRegistration,
)


def _minimal_registration(**overrides) -> SourceRegistration:
    """Smallest legal SourceRegistration for an ACTION source."""
    base = dict(
        name="test.action",
        schema=dict,
        default_mode=SignalMode.ACTION,
        allowed_modes=frozenset({SignalMode.ACTION}),
        handler=lambda payload: None,
        log_redaction=RedactionPolicy(summarize=lambda p: ""),
    )
    base.update(overrides)
    return SourceRegistration(**base)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_require_constitution_echo_defaults_to_false():
    """Backwards-compat: existing claude_code COGNITION sources must NOT
    suddenly require the new phantom-tool receipt path. Default `False`
    keeps legacy behavior; codex/local sources opt in via the validator
    in sovereign."""
    reg = _minimal_registration()
    assert reg.require_constitution_echo is False


def test_prompt_template_format_defaults_to_claude_code():
    """Default matches the existing in-agent dispatcher path
    (process_input)."""
    reg = _minimal_registration()
    assert reg.prompt_template_format == "claude_code"


def test_constitution_injection_defaults_to_none():
    """Default `"none"` matches today's ARTIFACT reality where most
    sources construct prompts internally without doctrine injection."""
    reg = _minimal_registration()
    assert reg.constitution_injection == "none"


def test_system_prompt_budget_bytes_defaults_to_none():
    """None signals 'use operator default'; per-source override is
    allowed but not implied."""
    reg = _minimal_registration()
    assert reg.system_prompt_budget_bytes is None


# ---------------------------------------------------------------------------
# Backwards compatibility — pre-0.11 callers
# ---------------------------------------------------------------------------


def test_pre_0_11_caller_constructs_cleanly():
    """A registration that doesn't mention any of the new fields must
    still construct successfully — that's the backwards-compatibility
    guarantee documented in the changelog row of v1.4 of the design."""
    reg = _minimal_registration()
    assert reg.name == "test.action"
    assert reg.handler is not None
    assert reg.log_redaction is not None


# ---------------------------------------------------------------------------
# Per-field setting works as expected (the SDK side; semantic enforcement
# lives in the sovereign-side registry validator, not here)
# ---------------------------------------------------------------------------


def test_codex_reviewer_can_set_require_echo_true_at_sdk_level():
    """The SDK accepts any combination of fields; semantic enforcement
    (e.g. 'codex format requires require_constitution_echo=True') is the
    sovereign-side registry validator's job."""
    reg = _minimal_registration(
        prompt_template_format="codex",
        require_constitution_echo=True,
    )
    assert reg.prompt_template_format == "codex"
    assert reg.require_constitution_echo is True


def test_local_reviewer_format_settable():
    reg = _minimal_registration(prompt_template_format="local")
    assert reg.prompt_template_format == "local"


def test_bare_format_settable_for_caller_responsibility():
    """`bare` is the caller-responsibility format used by hosts that wire
    their own verification (e.g. workflow gates that drive the canary
    inspection themselves)."""
    reg = _minimal_registration(prompt_template_format="bare")
    assert reg.prompt_template_format == "bare"


def test_constitution_injection_full_settable():
    """ARTIFACT or COGNITION sources opt in to dispatcher-driven
    doctrine injection by setting `constitution_injection="full"`."""
    reg = _minimal_registration(constitution_injection="full")
    assert reg.constitution_injection == "full"


def test_system_prompt_budget_override_settable():
    """Per-source override is a positive integer byte budget."""
    reg = _minimal_registration(system_prompt_budget_bytes=8192)
    assert reg.system_prompt_budget_bytes == 8192


# ---------------------------------------------------------------------------
# Field types — Literal narrowing protects implementers from typos at
# static-analysis time. Runtime type-checking is deliberately NOT done
# here (matches the rest of the dataclass; trust the type checker).
# ---------------------------------------------------------------------------


def test_field_annotations_are_present():
    """Sanity check: the new field names exist on the class. Catches
    refactors that accidentally drop a field without updating tests."""
    annotations = SourceRegistration.__annotations__
    assert "require_constitution_echo" in annotations
    assert "prompt_template_format" in annotations
    assert "constitution_injection" in annotations
    assert "system_prompt_budget_bytes" in annotations
