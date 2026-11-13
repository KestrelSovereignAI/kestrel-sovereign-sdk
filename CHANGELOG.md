# Changelog

All notable changes to this project are documented in this file.

## Unreleased

## 0.53.0

### Added

- Complete standalone request-lifetime ownership for streaming HTTP routes,
  including prefetch, response execution, original source retirement and
  explicit host-policy injection. FastAPI preserves the actual ASGI root;
  tenant and compatibility routes use the same acquisition mechanism.
- Request roots retain delivered original failures, including generator-finally
  failures during natural exhaustion; protocol projection is not a successful
  source-lifetime receipt even if a subsequent `aclose()` succeeds.
- Terminal `StreamRefusal` values keep logical protocol results separate from
  failed source lifetimes. Modern HTTP drains the actual source and joins its
  retirement before projecting the error; late finalizer failures still win.
  Deliberately unsupported profiles reject the value rather than serialize an
  empty protobuf. This is an opt-in handler value, not an exception whitelist
  or physical-retirement acknowledgement.
- Compatibility gRPC coroutine writers retain their original source through
  all writes; cancellation or an observed close failure is not successful
  retirement. Native hosts explicitly supply their admission/lifetime policy;
  copied context alone does not admit or retire work.
- Exact-Git-tree regression import via `--tests-since`, with canonical test
  namespace relocation, retained reviewed test paths and required package
  ancestors, not dirty vendored bytes or all upstream fixtures.

### Fixed

- Late subprocess spawn completion re-evaluates its exact retained child after
  joining an older incomplete retirement attempt, instead of losing the only
  autonomous cleanup wakeup. The actual original child is terminated and reaped.
- Test diagnostics track completed pytest progress instead of healthy total
  wall time while retaining the original native diagnostic sink through thread
  join and interpreter shutdown. Accelerated regression profiles begin after
  cold collection; normal production startup diagnostics remain unchanged.

### Migration

- The base SDK and existing wire types remain unchanged. Native Core adapters
  using the new factory/refusal contracts require this release and their
  coordinated Core dependency floor. Source merge is not operator rollout,
  live Kite acceptance, or proof of cloud cold-start time/cost.

## 0.52.2

### Added

- Shared native original-source handoff and body primitives for host adapters:
  per-frame ACK/Stop, writer-safe serialized emissions, original-task factory/
  iterator acquisition, retained partial resources and original retirement errors.
  These primitives grant no authority, create no Tasks and do not supply a Task
  admission or physical-join receipt. Hosts must supply that boundary. Native
  HTTP request/response lifetime integration remains separate work.

## 0.52.1

### Fixed

- Native JSON-RPC and REST error projection preserves the original registered
  host-control exception, including compound failures and failures after the
  first streamed event, original body-decoding failures, and errors that cannot
  safely be formatted. Native telemetry and error diagnostics classify control
  before recording it; telemetry skips attribute extraction after control and
  preserves registered errors from the extractor itself, queue shutdown, and
  compound groups containing cancellation. Bare cancellation remains outside
  host classification. Enabled legacy REST body/query normalization observes
  the same original-error boundary. Ordinary protocol errors keep their existing wire
  behavior. Classification uses the existing host policy; no Core dependency or
  new wire extension is introduced. Incremental host transport, deadlines and
  caller-withdrawal ownership remain separate host responsibilities.
- The SDK test driver's original diagnostic file descriptor remains available
  through native interpreter shutdown without an unclosed TextIO finalizer.
- Test fixtures close their original event loops and HTTP clients, and local
  fixture Git commits use the canonical project identity.

## 0.52.0

### Changed

- `kestrel_sdk.native_a2a` moves to upstream a2a-python `main` at `2cc1dd5`
  (`1.3.0-21-g2cc1dd5`), picking up upstream's server and v0.3 compatibility
  fixes since `11f1116`: cancellation metadata reaches `AgentExecutor.cancel`,
  input modes match on type and subtype, event queues close when a task
  fails to start, and `after_handler_run` runs for cancelled calls.

### Removed

- Breaking: `kestrel_sdk.native_a2a.utils.versioning` is removed from the
  vendored A2A SDK's Kestrel patch. Use upstream's own
  `kestrel_sdk.native_a2a.client.client_factory.major_minor`, which has the
  same semantics, or `packaging.version` directly. Kestrel Core stopped
  importing the shim in KestrelSovereignAI/kestrel-sovereign#4005. The
  remaining patch covers HTTP+JSON error handling and `A2A-Version` carriers.

## 0.51.0

### Changed

- Breaking: `GovernedToolBinding.invoke` requires the keyword `operation_id`.
  A caller creates a bounded opaque identifier once for an intended operation
  and reuses it only when resubmitting that operation. Hosts bind it to the
  caller, agent, stable tool registration and arguments before dispatch. The
  identifier carries no execution authority and does not prove an outcome.
- Decorated feature tools preserve `ResourceOutcomeUnknown` and its original
  cause, so a host can journal an uncertain effect instead of treating it as
  an ordinary tool rejection and continuing with success narration.

## 0.50.0

### Changed

- `kestrel_sdk.native_a2a` moves from upstream a2a-python 1.2.3 to upstream
  `main` after 1.3.0 (`11f1116`) and converges on it. Where upstream fixed a
  defect Kestrel had also patched, upstream's fix replaces Kestrel's. Agent
  Card signing and verification, response decoding, JSON-RPC envelopes,
  client selection and server version matching now behave as upstream. The
  remaining Kestrel patch is listed in the README. `VENDORED.json` records
  the base as `1.3.0-12-g11f1116`. The wire protocol remains A2A 1.0.1.
- Breaking: Agent Card signatures cover upstream's canonical form, which
  omits empty strings, lists and objects. A card that contains an empty
  value, such as a security requirement with no scopes, verifies here but
  not in SDKs that canonicalize the served JSON unchanged. Publish cards
  without empty values.
- Breaking: a key provider error that is a PyJWT `PyJWTError`, including
  `PyJWKClientConnectionError`, now skips that signature instead of
  propagating. Server version validation accepts any `1.x` client version,
  and the client factory may select an A2A 0.3 interface when a card offers
  one.

## 0.49.0

### Changed

- LLM contract version 8 declares `LLMResponse.stop_reason`, appended after
  the existing fields with a `None` default. Adapters preserve the provider's
  non-empty string literal and case; unknown, blocked and output-limit
  reasons remain evidence for the host to judge. Text, usage, tool calls and
  EOF must never manufacture a successful reason.
- Every normally completed stream, including plain-text and empty streams,
  must yield exactly one final `LLMResponse` as its last item. Its content is
  assembled metadata, not another chunk to render. Errors and cancellation
  propagate; native streams close before the terminal is yielded.
- Plain streams reject nonempty requested tools before dispatch and reject
  unexpected returned tool evidence. Use `get_streaming_response_with_tools`
  for tool calls. Its terminal is now required even without tool calls.
- Both public conformance drain helpers enforce one shared terminal-order
  implementation and check native stop metadata without classifying success.
  Dataclass construction remains permissive, as for existing fields.

### Added

- Public lightweight `kestrel_sdk.llm.LLMProtocolError` for provider protocol
  contradictions. Hosts must classify it as a failed generation. Migration:
  populate `stop_reason` directly and update plain streams and their tests to
  include a terminal; do not import Core's private completion helper. See
  [the completion contract migration](docs/llm-completion.md).

## 0.48.0

### Added

- `kestrel_sdk.tools.coerce_tool_arguments(tool_name, parameters, arguments)`
  is the public form of the `@tool` wrapper's argument coercion (#78), so a
  host's tool wrapper coerces exactly as the SDK's does. It returns
  `(arguments, error)`. `kestrel_sdk.features.base._coerce_tool_arguments`
  remains an alias to the same function object.
- `kestrel_sdk.features.host_errors` lets a host classify execution-control
  errors. After `register_control_error_predicate(fn)`, the SDK
  `DynamicTool.execute` re-raises the original exception, unchanged, whenever
  `is_host_control_error(exc)` is true, instead of turning it into a failure
  dict. The host owns the predicate; the SDK never decides what counts as
  control. With none registered nothing is control, so standalone behaviour is
  unchanged. Registration is process-global: the same function again is a
  no-op, a different one raises `RuntimeError`, and a predicate must return a
  `bool`.
- `kestrel_sdk.logging_summary.content_log_summary(text)` describes caller
  text for a log line as `chars=N digest=<12 hex>` (a process-keyed HMAC),
  mirroring Core's helper of the same name.

### Changed

- **Behaviour change for `KESTREL_DATA_KEY_FILE` passphrase deployments without
  a writable salt location** (for example a read-only secret mount with no
  `<key file>.salt`, `KESTREL_DATA_KEY_SALT` or `KESTREL_DATA_KEY_SALT_FILE`).
  New writes no longer fall back to the legacy unsalted SHA-256 key: `encrypt`,
  `get_agent_key`, `get_master_key_bytes` and `encrypt` on the ciphers from
  `get_fernet` / `get_agent_fernet` raise `MasterKeyNotConfiguredError` naming
  the remedy. Decryption still reads existing legacy ciphertext. Set
  `KESTREL_DATA_KEY_SALT`, point `KESTREL_DATA_KEY_SALT_FILE` at an existing
  salt file, or make the key file's directory writable. Env-only
  `KESTREL_DATA_KEY` passphrases keep their documented legacy mode and warning.
- An explicit `KESTREL_DATA_KEY_FILE` that is missing, unreadable, empty or not
  UTF-8 raises `MasterKeyNotConfiguredError` naming the path. It no longer
  falls through to `/run/secrets/kestrel_data_key` or `KESTREL_DATA_KEY`.

### Fixed

- The sibling `<key file>.salt` is published atomically and never replaced: it
  is fsynced in a private (`0o600`) temp file and hard-linked into place.
  Concurrent first starts adopt the first published salt, so no process can
  strand ciphertext under an overwritten salt, and readers never see a partial
  file. A temp file left by an interrupted creation does not block the next
  start. Creating the salt now requires hard-link support in that directory;
  otherwise configure the salt explicitly as above.
- Each process syncs the salt's directory before first encrypting with an
  automatic `<key file>.salt`, whether it published that salt or found one
  another process had linked but not yet synced. A crash can therefore no
  longer lose a salt that ciphertext was already written under. If the
  directory cannot be synced, new writes and key export raise
  `MasterKeyNotConfiguredError`; point `KESTREL_DATA_KEY_SALT_FILE` at that
  salt file or set `KESTREL_DATA_KEY_SALT` to its contents.
- Decryption never creates or syncs a key-file salt. `decrypt` and the ciphers
  from `get_fernet` / `get_agent_fernet` try a readable `<key file>.salt`,
  even one not yet durable, and then the legacy key, so existing ciphertext
  stays readable while new writes are refused.
- The SDK tool wrapper no longer writes caller content into logs. A tool's
  exception is logged as the tool name, exception class and
  `content_log_summary` of its message; a rejected argument as the tool,
  parameter and expected type, never the received value. The rejection record
  now comes from the `kestrel_sdk.tools.base` logger. Model-facing error text
  is unchanged.

## 0.47.0

### Added

- `DatabaseBackend.owns_open_transaction` exposes current-task transaction
  custody. The default is `None` (unknown); credential and external-effect
  publishers must require exactly `False` before starting a transaction that
  must commit independently. Core's native backends already provide this
  ownership check.

## 0.46.0

### Added

- `ToolPublicationContext` identifies the host-selected agent and chat session
  during a private `DeferredToolResult` publication callback. The context
  follows wrapper transfers and is cleared on every exit, including control
  errors. Headless calls receive no destination. The optional
  `ToolPublicationHost` protocol's `supports_tool_publication_context` flag
  promises completed policy and required audit before
  supplying this metadata; HTTP reader and tool authorization remain separate.

## 0.45.0

### Added

- Optional environment acquisition-custody API version 1 keeps original private
  create bytes and worker identity on the host's existing encrypted request row.
  Exact request/quote/attempt checkpoints support one atomic first dispatch and
  factual unknown-result reconciliation without retry authority. Explicit private
  storage preserves bounded bytes while generic dumps omit them. This source
  does not register a provider or implement a concrete human-control channel.
- Acquisition private fields require exact immutable bytes, including in the
  storage codec, so size bounds and restart digests use the same bytes. Mutable
  buffers and other wrapped types are refused before persistence.

### Fixed

- Specify repeated pre-open revocation: the same request ID reconciles its
  original result; a new request ID for the same permanently fenced target gets
  its own correlated tombstone with the original fence time. Changed targets
  still conflict. Distinguish a wrong response request ID from a wrong target.

## 0.44.1

### Added

- `DeferredToolResult` retains a private render-parts factory outside its JSON
  envelope. Supporting hosts finalize it once after post-tool policy completes,
  withholding private delivery if policy denies it, changes the public result
  or replaces the result object. Copies and
  serialization cannot duplicate the private delivery capability. This is a
  local host contract; it does not provide tool permission or audit confirmation.
- The SDK tool wrapper transfers private delivery while adding its canonical
  metadata and buffered parts. Finalization requires the actual post-hook
  object and an explicit boolean policy decision, and reports withholding
  separately from an empty delivery. A typed optional host protocol identifies
  the capability on the agent passed to a feature.

## 0.44.0

### Added

- Optional human computer control contracts bind an authenticated host-resolved
  consumer to one fixed session/control-epoch lease. Private human observations,
  canonical input wrappers and original receipts remain separate from agent
  operations. Pure validation helpers cover owner/consumer/session correlation,
  capability/frame/deadline bounds and original-epoch retirement. Concrete
  transport support and publication are separate delivery gates.
- Human action checks accept pixel-free frame records. Pre-open revocation has
  an explicit durable tombstone result, and original-epoch revocation uses
  `control_epoch`. Direct SDK resource validation suppresses field input details;
  non-SDK enclosing models and TypeAdapters must configure the same hiding.
  Invalid tags and unknown field names can still appear in error text; log
  exception classes only.
  Detailed Pydantic error payloads and content digests remain private. Correlation
  rejects copied lease/request mismatches, and shared UTC digest normalization rejects offset overflow as a
  validation failure. Non-whole-minute datetime offsets normalize to UTC during
  computer/human/workspace validation, preserving the original instant
  and digest across durable round trips; whole-minute representations stay unchanged.

### Fixed

- Source archive inclusion uses root-anchored paths, excluding local virtual
  environment license files from builds made in a developer checkout.
- Base Pydantic minimum is 2.4.0: earlier versions cannot reliably validate
  strict nested resource records through JSON. The optional A2A/test profiles
  retain their existing Pydantic 2.11.3 minimum. Verified for 0.44.0 on Python
  3.11: Pydantic 2.4.0 base profile and 2.11.3 full profile.

## 0.43.0

### Added

- `ToolSchema.model_visible` declares model exposure independently of the
  tool's input schema and permissions. It defaults to true and requires a
  boolean. Hosts retain hidden tools for other governed callers.
- `GovernedToolBinding` describes a host-owned reference to the actual retained
  dynamic registration, with target-currentness diagnostics and governed
  invocation. A binding grants no permission and must not be serialized.

## 0.42.4

### Fixed

- The optional `[tls]` extra provides one `certifi>=2026.10.23` policy for the
  `[a2a]`, `[httpx]`, `[httpx2]` and `[tracing]` profiles. Older certifi pins
  cannot resolve with these profiles; future bundle updates remain eligible.
  HTTPX2 retains its operating-system trust store through truststore.
  Base dependencies and packaged runtime sources are unchanged.

## 0.42.3

### Fixed

- Native HTTP+JSON protocol errors use `application/a2a+json`, as recommended by
  A2A 1.0.1. Unary and pre-stream errors preserve their Google RPC Status and
  ErrorInfo payloads. JSON-RPC keeps its binding media type, and authorization
  exceptions preserve their HTTP status and challenge headers. The retained
  optional v0.3 routes share this protocol-error renderer and its media type;
  Kestrel hosts leave those routes disabled. Addresses the
  shared SDK part of [Core #3812](https://github.com/KestrelSovereignAI/kestrel-sovereign/issues/3812).
- Regenerated the modified SDK's source/test patch and custody manifest from
  the pinned public base and verified packaged runtime sources.

## 0.42.2

### Fixed

- Native JSON-RPC and HTTP+JSON handlers accept the standard `A2A-Version`
  query parameter in unary and streaming requests. Service parameter names
  are case-insensitive, and all supplied versions must match the supported
  Major.Minor release before task access or execution. Conflicting values are
  rejected across headers and repeated query parameters; patch differences remain
  compatible. Fixes [#92](https://github.com/KestrelSovereignAI/kestrel-sovereign-sdk/issues/92).
- Rejected version values are escaped in protocol errors and warning logs;
  non-ASCII lookalikes are not recognized as standard service parameter names.
- HTTP request contexts retain Starlette's read-only header mapping so repeated
  header values remain available during negotiation.

## 0.42.1

### Fixed

- JSON-RPC and HTTP+JSON server handlers now require the Major.Minor protocol
  release from the `A2A-Version` header in unary and streaming requests. They reject unsupported minor releases
  with VersionNotSupportedError and continue to ignore specification patch numbers.
  Client interface selection and server validation share one version parser.
  The retained optional gRPC handler does not negotiate `A2A-Version` metadata;
  that existing gap is tracked in [#91](https://github.com/KestrelSovereignAI/kestrel-sovereign-sdk/issues/91).

## 0.42.0

### Added

- Reviewed official A2A Python SDK snapshot in `kestrel_sdk.native_a2a`, with
  signing, authorization, borrowed HTTP-client ownership, Agent Card discovery and response-decoding
  corrections. Apache license, source/test patch and reproducible custody
  metadata ship in both distributions. The wire protocol remains A2A 1.0.1.

- Added `CommandParseError` (`kestrel_sdk.tools`), a `ValueError` whose
  message names the command and the problem in the input.

### Changed

- `[a2a]` installs the vendored SDK's runtime dependencies instead of the external
  `a2a-sdk` distribution. `Feature.get_agent_skills()` constructs its native
  AgentSkill from the same implementation hosts consume. Base SDK imports and
  dependency requirements remain lightweight.

- The native client factory selects only A2A 1.0 interfaces, including patch
  revisions. It rejects unsupported advertised protocol versions before opening
  a transport instead of automatically selecting upstream 0.3 compatibility code.
- Agent Card discovery uses the same bounded-error JSON/Protobuf decoder as
  native responses. A malformed signature cannot hide a later valid signature;
  verification-key lookup failures remain visible to the caller. Providers reject
  individual unknown or untrusted keys with `PyJWKError`; other lookup failures
  propagate rather than being mistaken for invalid signatures.

- `AgentTool.parse_command_args` now binds `!command` arguments differently
  (#73, kestrel-sovereign#3118). Previously it split the text on whitespace
  and bound words by position, and the last parameter, plus any `object` or
  `array` parameter, took all remaining words. A free-text parameter that was
  not last therefore broke on its second word. For example,
  `!strategy-search context window bloat` bound `kind="window"`. JSON
  containing spaces in a non-final string parameter (`args_json`) was split
  across the parameters after it. The docstring promised quoting, but the code
  did not implement it. The new binding:
  - is quote-aware. A value that starts with `"` or `'` may contain spaces and
    ends at the matching quote; inside double quotes, `\"` and `\\` are
    escapes. Quotes inside an unquoted word stay ordinary text, so
    `don't` and `{"repo":"x"}` arrive verbatim. Plain POSIX `shlex` would
    reject the first and strip the quotes from the second.
  - accepts `name=value` (with quoted values, such as
    `args_json='{"intent": "x y"}'`) when `name` is exactly a declared
    parameter. Any other `a=b` word, and any quoted token, stays a positional
    value. Positional values come first, then `name=value`.
  - gives each parameter one value. No parameter takes the remaining words,
    so a multi-word value must be quoted or named. Commands made of
    single-word positional arguments bind exactly as before. For the same
    reason, unquoted words that fit the declared parameters still bind one
    per parameter. On the four-parameter `strategy_search`,
    `!strategy-search context window` still binds `kind="window"`; only
    words beyond the last parameter can be detected and refused.
- Inputs that were previously misbound or silently accepted now raise
  `CommandParseError`. These are: more values than parameters (including any
  word given to a tool with no parameters, which used to be dropped), an
  unbalanced quote, text directly after a closing quote, a parameter given
  twice, a positional value after `name=value`, and an `object` or `array`
  value that is not JSON of that shape (it used to be passed on as a string).
  Integer, number, and boolean values still fall back to the typed string,
  and the `@tool` wrapper rejects them with the parameter's name.
  Command-surface callers must catch `CommandParseError` and show its message
  as the command's failure.

## 0.41.0 — 2026-10-22

### Added

- Authoritative `ArtifactService.head` lookup for conflict resolution; history
  timestamps and revision IDs are not a substitute for the committed head.
- Bounded workspace transfer contracts and explicit provider/driver configuration
  factories for portable saved-output import/export (#86).
- Versioned visual computer-use contracts: separate environment provisioning
  and driver protocols, owner scopes, bounded leases, pinned image requests,
  control epochs, observations and action receipts. No browser/cloud runtime
  dependencies are added. These types do not enable computer access themselves.
- Producer-neutral artifact references, immutable manifests/revisions,
  compare-and-swap publication and an authenticated service protocol. Saved
  artifacts have independent identity and lifetime; run/session provenance is
  optional. Existing operator-run metadata remains a separate consumer until
  its coordinated host migration.

- Bind staged artifact files to the future publication key, so completing one
  save cannot consume another pending save’s bytes (#86).

## 0.40.0 — Tagged; publication pending

### Changed

- Replace feature-level draft Agent Cards with `Feature.get_agent_skills()`,
  returning official A2A SDK messages from the currently exposed tool schemas.
  A2A remains an optional dependency. Tool decoration no longer duplicates
  schema metadata in a protocol model.
- Validate required skill fields with the official SDK before returning a
  capability list; invalid metadata raises the native validation error.
- Remove the handwritten draft `kestrel_sdk.a2a` wire types. Hosts and consumers
  must use the official `a2a.types` models and upgrade together.

## 0.39.2 — 2026-10-18

### Fixed

- Canonicalize outbound URL authorities with IDNA 2008 before DNS so validation,
  native HTTP clients, and TLS identify the same domain. Returned URLs and host
  names use the canonical ASCII form. Require IDNA-valid domain names, including
  ASCII names; names with underscores, reserved hyphen patterns or invalid
  A-labels are now refused before DNS. Reject URL user information, raw control
  characters, surrounding whitespace, and validated records whose origin
  metadata disagrees with their URL. IDNA is a lightweight
  base dependency; HTTP clients remain optional extras.
- Inspect native request hosts in their raw ASCII form so malformed A-labels
  consistently raise `SSRFError` in both HTTP client families.
- Refuse invalid IPv4-shaped authorities before DNS, including leading-zero
  spellings that native clients reject. IP-literal records cannot approve a
  different public address. Suppress unbounded parser/IDNA input from expected
  validation-error tracebacks.
- Reject unencodable UTF-8 URL suffixes before DNS rather than allowing a
  native request-construction exception after validation.
- Preserve authority host kinds: unsupported bracketed literals are refused
  rather than rewritten into DNS names. Refuse IPv6 interface/zone hints in
  public destinations, including IPv4-mapped addresses, before native clients
  can receive non-ASCII or unbounded scope identifiers.
- Refuse misplaced or trailing literal brackets before authority normalization;
  parsing components alone must not authorize rewriting a different host.

## 0.39.1 — 2026-10-17

### Added

- Shared outbound URL validation and DNS-pinned transports in
  `kestrel_sdk.security.outbound`, extracted from Sovereign core so host
  fetchers and remote MCP connections use one policy. Validation rejects
  non-public IPs, including mixed public/private DNS answers. The optional
  `httpx` and `httpx2` extras provide each client's native transport; the base
  SDK still requires only Pydantic. Transports bind the scheme, host and port,
  dial approved IPs, and retain the original HTTP Host and TLS identity.
  Pools preserve native finite limits, a single connect deadline covers the
  address set, and direct validated-record construction enforces the same
  address policy. SSRF refusals join the shared security exception hierarchy.

## 0.39.0 — 2026-10-02

### Added

- Added the decisions contract (`kestrel_sdk.llm.decisions`, LLM contract
  version 7). A decision is a set of typed `choice` / `score` / `noul`
  questions evaluated against a `state`, answered with probability
  distributions instead of text. This is the `/v1/systemone` shape served by
  TypeSafe Jev, OpenRouter, Ollama (≥ 0.35) and others. It provides:
  - request, answer and result types, plus typed errors and rejection
    reasons;
  - `validate_decision_request()`, which returns an immutable, size-measured
    `ValidatedDecisionRequest` snapshot. Adapters only ever see the snapshot,
    so a caller mutating its own `state` mid-call cannot change what is sent;
  - validation bounds, set to the intersection every known route accepts:
    1–64 questions, 2–26 options or levels, bounded text, a strict JSON tree
    for `state` (string keys, finite numbers, no cycles, depth ≤ 64), and a
    1 MiB limit on the whole request;
  - `concentration()`, the single definition of how peaked a distribution is.
    Vendor `confidence` fields are intentionally not part of the contract.
- Added `LLMAdapter.adecide()` and `LLMAdapter.list_decision_models()`. They
  default to raising `DecisionsNotSupported` and returning an empty list, so
  existing adapters keep conforming.
- Added `ProviderCapabilities.supports_decisions`, as the last field so
  positional constructions are unaffected.
- Added decision metrics, separate from the chat LLM series:
  `kestrel_llm_decision_calls_total`, `kestrel_llm_decision_duration_seconds`
  and `kestrel_llm_decision_tokens_total`.

### Changed

- Operator artifact metadata, inference-lease public metadata and decision
  state now share one bounded deep-freeze (`kestrel_sdk._frozen_json`);
  previously each carried its own copy. Each contract keeps its own key,
  string and number policy. A self-referencing lease metadata value now fails
  with a `ValueError` naming the cycle; it previously failed with a
  `RecursionError`.

## 0.38.2 — 2026-09-15

### Added

- Added `ToolParameter.nullable`, recording whether a parameter's signature
  admits `None` (`Optional[int]`, `int | None`, or a literal `None` default).
  `@tool` already computed this and discarded it. The field is last and
  defaults to `False`, so existing constructions are unaffected, and it does
  not change the schema advertised to the model.

### Fixed

- Coerced `@tool` arguments to the JSON type their schema declares before the
  feature method is called. A model that sent an integer as `"30"`, a boolean
  as `"true"`, or a number as `"1.5"` previously reached the method as a
  string, so any arithmetic or comparison on it raised from inside the feature
  (`min("30", 100)`). The wrapper now applies the same rules as the
  command-prefix parser, and a value that cannot be coerced returns a failed
  `ToolResult` naming the parameter and its expected type instead of leaking a
  `TypeError`. String, object, and array parameters pass through unchanged.
- Refused a JSON `null` sent for a non-nullable numeric or boolean parameter,
  which reproduced the same failure one line later (`min(None, 100)` raises the
  `TypeError` that `min("30", 100)` did). Such a `null` now returns the same
  named failed `ToolResult`; parameters whose signature admits `None` still
  receive it.

## 0.38.1 — 2026-09-04

### Added

- Added the optional async `Feature` / `HostFeature`
  `prepare_context_clause_refresh()` lifecycle hook. Hosts await it before
  synchronously rendering and atomically committing a context-clause refresh,
  allowing feature caches to rehydrate after privacy or other host-owned
  configuration transitions without making prompt reads asynchronous.

## 0.38.0 — 2026-08-31

### Added

- Added a validated, versioned Two Axes contract fixture shared by Sovereign
  core and out-of-tree observability features. The fixture keeps a peer causal
  predecessor distinct from a spawned child's signed-lineage parent and pins
  fail-closed, cold-restart, backend, and load-order expectations.

## 0.37.1 — 2026-08-28

### Added

- Added lifecycle-owned `ContextClauseRegistration` declarations and no-op
  `Feature` / `HostFeature` getters so feature packages can contribute cached
  system-context clauses through the existing validated contribution boundary.

## 0.37.0 — 2026-08-25

### Added

- Added the public `INBOUND_PRODUCER_CAPABILITY` contract and
  `IsolatedFeatureService.advertise_inbound_producer()` so services can declare
  explicitly whether host idle retirement would remove an unmanaged poller or
  listener. Omitted declarations remain ambiguous for backward-compatible,
  fail-resident host policy.
- Added typed client accessors that normalize absent or malformed declarations
  to the fail-resident state and authorize idle retirement only for exact
  `False`.
- Froze producer ownership after successful initialize negotiation so a live
  config change cannot leave a stale retirement-safe declaration on the host;
  producer-state changes require restart and re-negotiation.

## 0.36.0 — 2026-08-10

### Added

- Added exact, versioned service discovery contracts and trusted operator
  context, execution-target discovery, and server-side target authorization.
- Added durable run, control, stage, attempt, external-job, and authorized
  artifact protocols for feature-owned operator execution.
- Added `Feature` and `HostFeature` contribution methods
  (`get_service_registrations()`, `get_wait_provider_registrations()`,
  `get_workflow_registrations()`, `get_feature_permission_defaults()`, and
  `get_setup_step_registrations()`) with canonical validated
  `contribution_owner` identities. The module-qualified implementation default
  is deterministic, independent of mutable feature names, distinguishes equal
  class names in separate packages and `_Foo` from `Foo`, and uses a bounded
  hash fallback for unusual names. The public active-set validator requires
  runtimes to reject duplicate owners before registration. The base classes do
  not claim or intercept the historic `owner` attribute, so existing
  subclasses may continue assigning it any value. Runtimes validate
  registration owner fields against the exact canonical identity and retain
  that identity and the exact returned objects for the active lifecycle.
- Defined browser security boundaries: clients provide opaque identifiers, not
  filesystem paths, commands, environment variables, credentials, or arbitrary
  secrets.
- These additions are SDK contracts only. Sovereign remains responsible for
  runtime registries, authorization, and lifecycle; Workflows owns durable
  state; and feature packages own execution engines and console panels.

### Fixed

- Made direct `RunRecord` authorization return the same typed not-found error
  for tenant mismatch as tenant-scoped absence, while preserving explicit
  authorization errors for visible-record freshness, action, boundary, and
  capability denials.
- Made `RunPage` reject exact and divergent duplicate run IDs without silently
  changing its records, while preserving the 100-record stored-tuple bound.
- Restored runtime-resolvable return annotations on all Feature and HostFeature
  contribution methods without importing contribution models during base
  module import.

## 0.35.1 — 2026-08-09

### Fixed

- Made isolated-feature startup and shutdown retain authoritative ownership of
  child processes, process waits, client cleanup, event delivery, and hostile
  cancellation paths until they settle or are safely fenced for a retry.
- Ensured TERM/KILL escalation is bounded and one-time, stream disposal follows
  process observation on Windows, and unresolved retirement cannot admit a
  replacement or retain decoded event payloads and handler secrets.
- Hardened SDK CI and PyPI publishing with Python 3.11–3.14 Linux/Windows
  coverage, exact artifact tests, immutable tag/SHA validation, pinned actions
  and build tooling, and a final non-OIDC release revalidation gate.

## 0.35.0 — 2026-08-03

### Added

- Private inference providers now implement an owner-scoped, idempotent
  ``touch`` operation. Core calls it for real LLM traffic so scale-to-zero
  runtimes renew their idle deadline without provisioning replacement capacity
  or exposing route credentials.

### Changed

- **Breaking for inference-lease providers.** ``InferenceLeaseProvider`` is
  runtime-checkable, so a 0.34.0 provider that does not implement ``touch``
  now fails ``isinstance`` against the protocol and is rejected where
  conformance is checked, rather than failing later at first call. Providers
  published against 0.34.0 must add the operation to load under 0.35.0.
- **Behavior change for inference-lease providers.**
  ``InferenceLease.validate_for`` now also enforces an absolute lease
  lifetime: ``expires_at`` may not exceed
  ``requested_at + ready_deadline_seconds + expected_session_seconds``.
  Equality is allowed; anything beyond raises
  ``InferenceLeaseConstraintError``. Previously the validator placed no upper
  bound on ``expires_at``, so a provider that returned a coarse, natural
  expiry — an hourly-billed GPU host handing back ``created_at + 1h`` against
  the default 900s + 900s bounds, for example — validated under 0.34.0 and is
  rejected under 0.35.0. Providers must clamp the expiry they report to the
  window the request actually authorized. This closes an authorization gap
  where a lease could outlive the session its cost and readiness bounds were
  approved against; it does not change quoting, acquisition, or release.

## 0.34.0 — 2026-08-02

### Added

- A provider-neutral, owner-scoped remote inference lease contract for private
  on-demand model serving. The additive contract includes immutable request
  constraints, pre-provisioning quotes, explicit lifecycle states, sanitized
  failures and telemetry, and an in-memory route whose endpoint and credentials
  cannot enter agent-facing serialization.
- The `kestrel_sovereign.inference_lease_providers` entry-point group constant
  and runtime-checkable provider protocol for external infrastructure packages.
## 0.33.0 — 2026-07-26

### Added

- Capability-negotiated private host ingress for isolated features. Services
  register named, bounded JSON handlers; hosts use the typed
  `call_host_ingress()` API only after a compatible capability advertises the
  name. Ingress remains outside `tools/*`, is invisible to agent tool
  inventories, validates JSON at both trust boundaries, offloads sync handlers,
  and redacts errors and payload values.

## 0.32.0 — 2026-07-25

### Added

- A capability-negotiated, typed execution-context contract for isolated
  `tools/call` RPCs. Hosts can attach trusted invocation, idempotency, retry,
  and trigger metadata without exposing it in user-controlled tool arguments.
- Public `ToolExecutionContext`, `ToolExecutionTrigger`, related capability
  types, and `get_tool_execution_context()` for task-local handler access.
  Context is cleared after every invocation, including failed or cancelled
  calls, and context use fails closed with legacy services.
