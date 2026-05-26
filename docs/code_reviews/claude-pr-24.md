# Claude Review: PR #24

- PR: https://github.com/KestrelSovereignAI/kestrel-sovereign-sdk/pull/24
- Reviewed: 2026-05-26T16:42:09Z

## PR Review: LLM Provider Capabilities Contract

### No blocking findings.

The PR is clean, well-structured, and backwards-compatible. Here's the detailed breakdown:

---

### Positive observations

1. **Backwards compatibility preserved** — `ProviderCapabilities` defaults are conservative (all `False`/`NONE`), `LLMAdapter.provider_capabilities()` has a default implementation, and `ProviderInfo.capabilities` uses `field(default_factory=...)`. Existing plugins that don't override anything continue to work unchanged.

2. **Contract version bump is correct** — `SDK_LLM_CONTRACT_VERSION` goes from 2 → 3, matching the addition of new surface area. The version history comment in `__init__.py:54-58` is clear.

3. **Enum resilience** — `_enum_value()` in `capabilities.py:113-124` gracefully falls back to the default on unknown strings, which is exactly right for a plugin contract where the SDK may add enum members before plugins update.

4. **`frozen=True` dataclass** — `ProviderCapabilities` is immutable, preventing accidental mutation bugs. Good choice.

5. **Test coverage is thorough** — defaults, round-trip serialization, unknown-mode fallback, per-instance identity, adapter override, and the contract version pin are all tested.

---

### Residual risks (non-blocking)

1. **`to_dict()` / `from_mapping()` asymmetry potential** — `to_dict()` uses `dataclasses.asdict()` which recursively converts, then manually fixes enums. If a future field is added that `asdict` doesn't handle well (e.g., a nested dataclass with enums), the manual fixup list in `capabilities.py:79-83` must be extended. Consider a comment noting this coupling, or a round-trip assertion in tests (the existing `test_to_dict_uses_wire_values` + `test_from_mapping_accepts_plugin_dicts` effectively cover this today, but don't assert `from_mapping(caps.to_dict()) == caps`).

2. **`ProviderInfo` is a plain `@dataclass`, not `frozen`** — `capabilities` field uses `default_factory` correctly, but since `ProviderCapabilities` is frozen, a consumer could still do `provider_info.capabilities = something_else`. This is consistent with how other `ProviderInfo` fields work (e.g., `selection_hints`), so not a real issue, just worth noting.

3. **No `__eq__` / `__hash__` concern** — `frozen=True` on `ProviderCapabilities` gives both for free via `dataclass`. The tuple fields (`model_dependent`, `notes`) are hashable. Clean.

4. **Duplicate contract version test removed** — `TestStreamingWithToolsContractVersion.test_contract_version_is_2` was relaxed to `>= 2` (`test_llm_contract.py:1002`). This is the right call — pinning the exact version in two places creates false failures on every bump. The canonical pin is now only in `TestLLMAdapterContractVersion.test_contract_version_is_3`.

5. **`__all__` is updated** — `__init__.py:80-91` exports all four new symbols. Confirmed no omissions.

---

### Verdict

**Ship it.** The contract extension is additive, defaults are safe, serialization is resilient to unknown values, and test coverage matches the new surface area. No breaking changes for existing plugins.
