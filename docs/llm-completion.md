# LLM completion contract version 8

SDK 0.49.0 makes provider completion evidence part of the public contract.
Populate `LLMResponse.stop_reason` directly with the provider's native
non-empty string literal, preserving case. Convert native enums to their
string value. If the provider reports no reason, leave `None`; never infer
completion from content, usage, tool calls, or EOF. Unknown, blocked and
output-limit reasons must survive unchanged. The host decides whether a
generation completed and whether any tool call is authorized.

```python
return LLMResponse(content=text, stop_reason=native_finish_reason)
```

Both streaming methods yield exactly one final `LLMResponse` on normal
completion, including plain-text and empty streams. It follows all text and
tool markers and carries assembled content, native stop metadata and usage.
Consumers must not render its assembled content a second time. Complete
owned-stream cleanup before yielding it. A transport error, protocol error,
cancellation or cleanup failure propagates; do not yield a fabricated
successful terminal from `finally`.

The plain method accepts only `None` or empty `tools`. Reject nonempty tools
before a vendor request, and reject unexpected returned tool markers or
assembled calls instead of stripping them. Use the with-tools method for
tool intent and assembled calls, retaining its existing marker order rules.

Raise SDK `LLMProtocolError` with a content-free rule description for a
provider protocol contradiction. Hosts classify it as an unfinished attempt;
transport exceptions and cancellation retain their own types. A native
terminal reason followed by additional content, or a conflicting terminal
reason, must not become a completed answer. Usage-only native trailers may
update counts before the single terminal envelope; they must not erase its
reason or create one.

Provider tests should use `assert_response_contract`,
`drain_streaming_with_tools` and `drain_streaming_text_only` from
`kestrel_sdk.testing`. Both drain helpers require the terminal as the last
item; the plain helper returns only concatenated text chunks. The helpers
accept `None` and unsuccessful/unknown reason literals as valid evidence
shape. They never decide that a generation succeeded. Dataclass construction
remains permissive; the conformance kit checks `None` or a non-empty exact
`str` and rejects enum objects and other metadata types.
