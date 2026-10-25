# kestrel-sovereign-sdk

Lightweight SDK providing base interfaces, protocols, and utilities for Kestrel Sovereign feature package development. Feature packages depend on this SDK instead of the full framework, keeping dependencies minimal and development fast.

## Voice provider contracts

`kestrel_sdk.voice` defines independent TTS, STT, and realtime conversation
provider contracts. Realtime providers declare capability metadata and mint a
provider-neutral browser bootstrap (WebRTC or WebSocket); voice IDs are scoped
to their provider. Tool-call batches pair every governed function result with
one continuation, while legacy single-result methods remain available for
older adapters.

## Private inference lease providers

`kestrel_sdk.llm` defines the infrastructure-neutral boundary used when an
agent requests bounded private inference capacity. The host quotes and selects
a provider before provisioning, then activates the returned route only after
the lease reaches `ready`. Provider packages register under
`kestrel_sovereign.inference_lease_providers`; they own infrastructure state,
while Kestrel core remains the single owner of active LLM routing.

```python
from decimal import Decimal

from kestrel_sdk.llm import (
    INFERENCE_LEASE_PROVIDER_ENTRY_POINT_GROUP,
    InferenceLeaseRequest,
    InferencePrivacy,
)

request = InferenceLeaseRequest(
    request_id="agent-turn-123",
    owner_id="did:kestrel:kite",
    model="qwen3:8b",
    runtime="ollama",
    privacy=InferencePrivacy.AUTHENTICATED_ENDPOINT,
    max_hourly_cost_usd=Decimal("0.75"),
    max_total_cost_usd=Decimal("0.50"),
)
assert INFERENCE_LEASE_PROVIDER_ENTRY_POINT_GROUP == (
    "kestrel_sovereign.inference_lease_providers"
)
```

`InferenceLease.to_public_dict()` is the lease's only agent-facing serializer. It
omits the owner, endpoint, API key, and secret headers. Host code may read the
`InferenceRoute` secret values in memory to configure `LLMService`; providers
must never put credentials in public metadata or failure messages.

## Visual computer use and durable artifacts

`kestrel_sdk.computer_use` separates environment provisioning from visual
session control and existing inference routing. `kestrel_sdk.artifacts`
defines saved deliverables with immutable revisions, conflict-aware publication
and ownership independent of the producing run or computer. These are typed
contracts, not enabled runtime capabilities. See the
[contract and host-enforcement guide](docs/computer-use.md).

## Installation

```bash
uv pip install git+https://github.com/KestrelSovereignAI/kestrel-sovereign-sdk.git
```

With encryption helpers:

```bash
uv pip install "kestrel-sovereign-sdk[crypto] @ git+https://github.com/KestrelSovereignAI/kestrel-sovereign-sdk.git"
```

The `[httpx]`, `[httpx2]` and `[a2a]` profiles select the shared `[tls]` extra,
which requires `certifi>=2026.10.23`. This uses the current post-incident CA
baseline and permits future bundle updates. See the
[CA-removal advisory](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2)
and [subsequent trust-store update](https://github.com/certifi/python-certifi/releases/tag/2026.10.23).

## Vendored native A2A implementation

The `[a2a]` extra supplies one reviewed, modified copy of the official A2A Python
SDK under `kestrel_sdk.native_a2a`. Hosts and feature skill producers use this
same implementation. The wire protocol remains A2A 1.0.1; this is not an official
upstream SDK release. The base SDK still requires only Pydantic and IDNA.

The vendored package includes its Apache 2.0 license, modification notice, public
base commit, exact reviewed source tree, source/test patch, and original/relocated
file digests in `VENDORED.json`. Run `uv run python -m scripts.vendor_a2a --check` from a
source checkout to verify custody offline. Reproduce updates from an explicit
Git tree with `--source`, `--revision` and `--base`; the importer refuses an existing
output directory and never consumes dirty working-tree files.

The snapshot contains signing, HTTP authorization, borrowed-client lifetime,
Agent Card discovery, response-decoding and HTTP version-negotiation fixes. Python imports and package-resource references are
relocated; protocol names, JSON fields and protobuf descriptors are preserved.
The repository maintains the upstream signing/JCS, client factory, response
decoding and HTTP authorization regressions under `tests/native_a2a`, including
independent canonicalization vectors. These are SDK regressions, not a claim
that Kestrel has passed the full A2A TCK.

Signature key providers return a key or raise PyJWT `PyJWKError` for an unknown
or untrusted key so verification can try the next signature. Other lookup
errors, including `PyJWKClientConnectionError`, propagate to the caller. A
peer-controlled mismatch between a signature algorithm and a returned key
rejects that signature while allowing a later valid signature to verify.

No runtime module alias, monkey patch, private protocol or signature fallback is
installed. A separately installed `a2a` Python package does not supply Kestrel's
SDK implementation. Full host conformance and interoperability remain host gates.

The native client factory accepts A2A 1.0 interfaces, including patch revisions,
and never automatically selects the upstream 0.3 compatibility transports. The
retained upstream `compat` modules are outside Kestrel's supported client API.

Python module relocation does not create a separate Protobuf registry. The
original protocol descriptors register their names process-wide; a separately
installed A2A SDK can conflict with them, particularly SDKs for older protocol
versions. Use the vendored implementation throughout a Kestrel process rather
than importing both SDKs.

Install `[a2a]` for the supported native HTTP and signing paths. Optional upstream
database, migration and gRPC modules retain upstream dependency-error messages
that mention `a2a-sdk[extra]`; those hints do not apply to this vendored package.
Install the named optional libraries directly when using those modules, with the
requirements recorded in `UPSTREAM-PYPROJECT.toml`. Installing the external SDK
to satisfy a vendored module's hint can create the descriptor conflicts above.
The retained gRPC server does not negotiate `A2A-Version` metadata
([#91](https://github.com/KestrelSovereignAI/kestrel-sovereign-sdk/issues/91)).
HTTP handlers accept `A2A-Version` in headers or query parameters, including
case-insensitive parameter names. Every supplied value must match the handler's
Major.Minor release; patch numbers are ignored. Empty values mean 0.3, so a
supported value cannot hide a conflicting or empty value in another carrier.

## Dependencies

- `pydantic>=2.0`
- `idna>=3.20,<4` for canonical IDNA 2008 host identity
- Optional: `cryptography>=42.0` (via `[crypto]` extra)
- Optional native HTTP clients: `[httpx]` or `[httpx2]`
- Optional native A2A models: `[a2a]`

### A2A skill contributions

Install `kestrel-sovereign-sdk[a2a]` when consuming
`Feature.get_agent_skills()`. It returns native `kestrel_sdk.native_a2a.types.AgentSkill`
messages derived from the feature's currently exposed tool schemas. Disabled
tools are excluded, and each call returns fresh messages. Ordinary feature
imports, tool decoration and execution do not import the optional A2A SDK.
Missing required skill fields fail through the official SDK validator before
any skill list is returned.

SDK 0.40 removes the handwritten draft protocol models under `kestrel_sdk.a2a`
and the feature-level `get_agent_card()` method. SDK 0.42 supplies the native
models under `kestrel_sdk.native_a2a.types`. The serving agent owns its signed Agent Card, transport URLs
and interface configuration; features contribute skills to that card. Upgrade
the host and its consumers together for this breaking change.

### Outbound HTTP

`kestrel_sdk.security.outbound` provides the shared server-side URL policy.
`await assert_safe_url(url, allowed_schemes=("https",))` resolves DNS off the
event loop and rejects every non-public address, including mixed DNS answers.
Pass the resulting `ValidatedOutboundURL` to `pinned_httpx_async_transport()`
or `pinned_httpx2_async_transport()` and use that transport with the matching
native client's `AsyncClient`. The transport dials only those approved IPs,
preserves the original Host and TLS identity, and refuses a different scheme,
host or port. Use a separate validated transport for a redirect destination;
disable automatic redirects when the caller should refuse them altogether.

Use the returned `validated.url` for the request. Domain authorities and `host` use
the same canonical ASCII IDNA 2008 name used for DNS, origin checks, HTTP Host,
and TLS identity. Unicode names such as `straße.example` retain their distinct
IDNA 2008 identity. Domain names must satisfy IDNA 2008, including ASCII input:
names such as `my_host.example`, `ab--cd.example`, leading/trailing hyphens, and
invalid A-labels are refused before DNS even when an HTTP client permits them.
IP literals, including bracketed IPv6 authorities, are supported separately;
IPv6 spelling and explicit numeric ports are normalized in the authority.
Bracketed authorities must be IPv6 literals; unsupported IPvFuture literals
are refused, and never reinterpreted as DNS names. Brackets must delimit the
entire literal host, followed only by an optional port. Malformed placement is
refused before authority normalization or DNS. Public fetches refuse IPv6
scope identifiers because local interface/zone hints do not identify a public
remote origin, including on IPv4-mapped IPv6 addresses.
Four numeric labels must be a valid IPv4 address; ambiguous leading-zero
spellings are rejected before DNS. A directly constructed IP-literal record may
approve only that literal's address.
URL user information, raw control characters, surrounding whitespace, and
unpaired surrogates that cannot be encoded as UTF-8 are rejected before DNS.
A directly constructed validated record must agree with its URL's
origin as well as its public IP set.

Validation itself needs no HTTP extra. A client dependency is imported only
when its transport factory is called.

The address policy combines Python's address classifications with the packaged
`outbound_policy.toml` for explicit IANA non-global allocations missing from
the supported standard-library tables. This includes the
[6a44 relay address](https://www.iana.org/assignments/iana-ipv4-special-registry).

The pool retains the matching native client's finite connection limits. One
connect deadline covers all approved addresses. These transports must be the
client's only transport: explicit `proxy` or `mounts` options select other
transports and bypass the pinning boundary. TLS uses each native client's trust
store and honors `SSL_CERT_FILE` / `SSL_CERT_DIR`; httpx uses certifi by default,
while httpx2 uses the operating system store.

## Usage

```python
from kestrel_sdk.features.base import Feature, Tool


class MyFeature(Feature):
    name = "my-feature"

    def get_tools(self):
        return [Tool(name="my-tool", description="Does something", handler=self.handle)]
```

### Tool argument types

A `@tool` method receives each argument as the JSON type its annotation
declares. Models routinely send an integer as `"30"` or a boolean as `"true"`,
so the wrapper coerces those to `30` and `True` before the call — the same
rules the `!tool arg` command-prefix parser uses. `str`, list, and dict
parameters pass through untouched. A value that cannot be coerced, such as
`"thirty"` for an `int`, never reaches the method: the call returns
`ToolResult.failed(...)` naming the parameter and the type it expects.

JSON `null` is judged the same way, because `min(None, 100)` fails exactly as
`min("30", 100)` did. A numeric or boolean parameter accepts `null` only when
its signature admits it — `Optional[int]`, `int | None`, or a literal `None`
default — which `@tool` records as the new `ToolParameter.nullable` field. A
`null` sent for a required `count: int`, or for `max_results: int = 30`, is
refused with the same named failure instead of reaching the method.

### Command arguments

`AgentTool.parse_command_args` binds the words after a tool's
`command_prefix` to its parameters. Each parameter takes one value, so a value
containing spaces must be quoted or passed by name:

```text
!strategy-search "context window bloat"
!strategy-search query="context window bloat" kind=blockers
!schedule deadline 2026-08-26T12:00:00+00:00 self_followup '{"intent": "x y"}'
```

- A value that starts with `"` or `'` runs to the matching closing quote. Inside
  double quotes, `\"` and `\\` stand for a quote and a backslash. Single quotes
  keep everything literally, which makes them the natural way to pass JSON.
- `name=value` binds by name only when `name` is exactly a declared parameter.
  Any other `a=b` word is a positional value, and so is any quoted token.
  Positional values come first, then `name=value`.
- A quote inside an unquoted word is ordinary text, so `don't` and
  `{"repo":"x"}` arrive verbatim.

When the input cannot be bound, the method raises `CommandParseError`
(`kestrel_sdk.tools`, a `ValueError`). This covers an unbalanced quote, more
values than parameters, a parameter given twice, a positional value after
`name=value`, and an `object` or `array` value that is not JSON of that shape.
Its message names the command and the actual problem. For a tool
`search(query: str, limit: int = 10)`, `!search context window bloat` fails
with `!search: too many arguments: it takes at most 2 positional values (query,
limit) but got 3 ('context', 'window', 'bloat'); quote multi-word values or use
name=value`. Callers should show that message as the command's failure.

Only words beyond the last parameter can be detected. Unquoted words that fit
the declared parameters still bind one per parameter, so on
`search(query, kind, limit)` the line `!search context window` binds
`kind="window"`. Quote the value or pass it by name.

## Operator execution contracts

`kestrel_sdk.operator` is the public contract surface for feature-owned
operator execution. It provides immutable data models and structural
protocols for:

- exact and stable SemVer-compatible service discovery with explicit host or
  agent scope;
- authenticated, tenant-bound, time-bounded `OperatorContext` authorization
  facts;
- browser-safe execution-target discovery and authorized target resolution;
- durable run, stage, attempt, control, and external-engine-job correlation;
- authorized artifact metadata whose `href` is the canonical same-origin
  authorization endpoint or a signed HTTPS URL, never a filesystem location;
- the asynchronous `RunService` implemented against the durable workflow
  state machine.

The canonical contracts most applications need are also available from the
top-level package:

```python
from kestrel_sdk import (
    ArtifactRecord,
    ExecutionTargetReference,
    OperatorContext,
    RunControl,
    RunLaunch,
    RunQuery,
    RunService,
    ServiceReference,
    ServiceRequirement,
)
```

Feature packages publish integration seams through the discoverable
`kestrel_sdk.features` surface. Both `Feature` and `HostFeature` expose
conservative no-op defaults, so existing feature subclasses do not need to
change:

```python
from kestrel_sdk.features import (
    ContributionContractError,
    ContextClauseRegistration,
    FeaturePermissionDefaults,
    ServiceContributions,
    SetupFlow,
    SetupStepRegistration,
    WaitProviderRegistration,
    WorkflowRegistration,
    normalize_setup_flow,
    validate_contribution_owner_uniqueness,
    validate_feature_contributions,
)
```

Agent features expose these through `get_service_registrations()`,
`get_wait_provider_registrations()`, `get_workflow_registrations()`,
`get_feature_permission_defaults()`, and
`get_setup_step_registrations()`, and `get_context_clause_registrations()`.
Host features use the same methods, tied to host start/stop instead of agent
enable/disable. Sovereign calls each contribution getter
exactly once per enable or host-start transition, validates every collection
and element with
`validate_feature_contributions(feature.contribution_owner, tool_names=...)`,
using the names from that feature's actual tools. The canonical
`contribution_owner` defaults deterministically to the implementation class's
module-qualified name, never to the mutable or inherited feature `name`. This
distinguishes equal class names from independent packages and `_Foo` from
`Foo`; unusual, nested, or overlong names receive a deterministic hash suffix
and remain bounded stable tokens. A feature that needs an identity independent
of code location may explicitly override the property with a stable token.
Before registering anything, the runtime must collect the exact owners for the
complete prospective set of simultaneously active agent and host features and
call `validate_contribution_owner_uniqueness(...)`; any duplicate rejects the
whole transition. It then retains those exact validated identities plus the
exact returned registrations, callables, and implementation objects for the
whole active lifecycle and uses them for disable or host-stop teardown. A
feature must therefore construct contributed objects once per instance and
return instance-stable objects. Every registration must declare the same
lifecycle `owner` as `feature.contribution_owner`; a type mismatch, duplicate
identity, duplicate active owner, or owner mismatch raises
`ContributionContractError` and the transition fails without partial
activation. The base classes do not define or intercept `owner`: existing
subclasses may continue assigning any legacy value to `self.owner`, including
display text, objects, or `None`, without affecting contribution identity.

`ServiceRegistration`, `WaitProviderRegistration`, `WorkflowRegistration`,
`SetupStepRegistration`, and `ContextClauseRegistration` all carry that
lifecycle owner. A context-clause registration retains a zero-argument string
renderer; Sovereign owns when it renders and how the integer priority affects
deterministic ordering and budget eviction. Before a host-owned configuration
transition rerenders the complete active context-clause set, Sovereign awaits
each contributing feature's optional `prepare_context_clause_refresh()` hook.
Features may override that async hook to load state that only became readable
after a privacy or other host policy change. The hook must update only the
feature's own renderer state and must not publish context clauses itself:
Sovereign invokes every synchronous renderer only after all preparation
succeeds, then atomically commits the complete batch. The base implementation
is a no-op, so existing feature classes remain compatible.

A workflow registration represents one actor identity and an immutable tuple
containing zero or more `SourceRegistration` values. Sovereign registers the
actor once, then registers each source without duplicating the actor. Source
names must be unique across all workflow registrations returned by one feature.

Setup-step `before` and `after` values are hard topological constraints across
the complete active step set. Unknown references and cycles reject the whole
transition with `ContributionContractError`; among steps currently eligible to
run, `(order, name)` is the deterministic tie-break. `SetupFlow` defines the
stable `setup` and `check` values. `normalize_setup_flow()` also accepts the
existing Sovereign `Flow` enum (including an `Enum` that does not inherit from
`str`) or its string value.

Workflow actors and setup steps may return a value directly or an awaitable.
The runtime must inspect every result and await awaitables (the SDK helper is
`await_contribution_result()`); silently dropping a coroutine is a contribution
contract violation.

Permission defaults are subordinate to authentication, capability, tenancy,
privacy, and other non-overridable policy gates. At the feature-permission
layer, `deny` rejects without prompting; `always_ask` prompts every invocation;
`ask` prompts unless a durable applicable decision exists; `session` may reuse
approval only in the current authenticated session; `allow` runs under an
explicit applicable grant; and `auto` uses Sovereign's automatic-policy path.
Sovereign must keep a parity gate covering the exact six SDK values and every
enforcement branch, rejecting unknown or incompletely mapped values.

These contracts establish permanent ownership boundaries:

| owner | responsibility |
| ----- | -------------- |
| SDK | Data models, protocols, validation, and declarative contribution shapes only. It owns no registry, persistence, authentication service, engine adapter, or lifecycle manager. |
| Sovereign (row 2) | Active registries, authentication and authorization enforcement, entitlement and target resolution, contribution registration/teardown, and feature lifecycle. |
| Workflows | Durable run state, idempotency records, stage/attempt history, controls, external-job links, and artifact records. Telemetry is not authoritative workflow state. |
| Feature packages | Execution engines, service implementations, workflow actors, setup steps, wait providers, and console panels. |

The browser receives opaque target IDs, bounded descriptors, capability names,
and authorized artifact links. Browser requests must never supply or
recover host filesystem paths, executable commands, environment variables,
credentials, or secrets. Sovereign resolves an authorized opaque target
server-side and feature-owned engines decide how that target is executed.

The canonical artifact URL is
`/authorized/artifacts/<opaque-artifact-id>`. The ID is an authorization lookup
key, not a filename: the endpoint rechecks the caller's tenant and artifact-read
authority on every request. Absolute artifact URLs are accepted only in the
validated signed-HTTPS form; the runtime must also enforce an explicit origin
allowlist and render external links with a safe browser policy that prevents
opener, credential, and referrer leakage. Service consumers must resolve an
exact `ServiceReference` or compatible `ServiceRequirement` for each immediate
operation and must not cache the returned implementation across feature
lifecycle changes.

Run launches bind tenant, source identity, boundary, target, and capability to
the trusted operator context. An agent-mediated request must use agent source
provenance; it cannot claim to be manual. The runtime assigns
`RunRecord.accepted_at` and advances `state_changed_at` plus the monotonic
`sequence` concurrency token; it does not accept a caller-created timestamp.
Every service method rechecks context freshness and its exact action. Launch
idempotency uses trusted tenant/action/key scope, while controls additionally
scope run and optional retry stage. A control may require an `expected_sequence`
compare-and-set precondition without adding that precondition to its idempotency
scope. Exact replay returns the original outcome; conflicting keys, illegal
transitions, and state races raise a typed conflict. Durable external-job and
artifact attachments require `run.attach`; `run.read` authorizes only run,
stage, and attempt reads, while artifact retrieval separately requires
`artifact.read`. Retry preserves the durable run ID and creates (or replays) a
stage attempt. Run discovery uses bounded `RunQuery`/`RunPage` cursors.
`RunPage` stores at most 100 records and rejects every duplicate `run_id`, even
when the repeated records are exactly equal. `RunRecord.authorize()` itself
turns a tenant mismatch into the same typed `RunNotFoundError` used for
tenant-scoped absence, so globally resolved IDs cannot create a cross-tenant
existence oracle. For a tenant-visible record, freshness, action, boundary,
and capability denials remain `OperatorAuthorizationError`. Stage and attempt
tuple listings are deterministically ordered and return only the first
requested bounded result set; unlike run discovery, they currently expose no
continuation cursor.

## Host features (host/fleet scope)

`Feature` **is a subagent** — each instance is bound to one agent (`self.agent`),
mounts its router under that agent's prefix, and can be called as a tool with its
own LLM context. `HostFeature` is the **host/fleet-scoped** sibling: it runs once
per host, has **no agent binding**, mounts its router at the host root, and lives
across host start/stop rather than agent enable/disable. It is what
`kestrel-sovereign` discovers and mounts, and what fleet-observability host
features implement.

```python
from kestrel_sdk import HostFeature, HostContext, UIContributions


class FleetObservability(HostFeature):
    name = "fleet-observability"  # stable slug for discovery / mounting
    capability = "fleet.observe"  # optional capability gate

    def get_router(self):
        # Mounted at the HOST ROOT — no agent prefix, no get_agent dependency.
        from fastapi import APIRouter

        router = APIRouter()
        # ... host-scoped routes ...
        return router

    async def on_host_start(self, ctx: HostContext):
        # Host-scoped store handle built on the SDK's OWN storage layer.
        # The feature layer (entities + a fleet TenantContext) is layered on
        # top of this handle — the SDK stays dependency-free.
        target = self.resolve_host_engine_target(ctx.config["host_db_url"])
        self.db = ctx.db
        await ctx.backplane.subscribe("fleet.events", self._on_event)

    async def _on_event(self, event):
        # Handle a live fleet event (persist, fan out to console, etc.).
        ...

    async def on_host_stop(self, ctx: HostContext):
        await ctx.backplane.close()

    def get_ui_contributions(self):
        return UIContributions(
            static_dir="/pkg/fleet/static",
            modules=["fleet-panel.js"],
            capability=self.capability,
        )
```

| aspect        | `Feature`             | `HostFeature`                        |
| ------------- | --------------------- | ------------------------------------ |
| scope         | one subagent          | host / fleet                         |
| binding       | `self.agent`          | none (`HostContext` at runtime)      |
| router mount  | under agent prefix    | host root (no prefix, no `get_agent`)|
| lifecycle     | enable / disable      | `on_host_start` / `on_host_stop`     |
| store         | agent store           | host backend under fleet tenancy     |
| called as tool| yes (A2A)             | no                                   |

`HostContext` is a minimal, `runtime_checkable` Protocol exposing the host `db`
backend, a pub/sub `backplane` handle, and host `config`. `UIContributions` is a
pure-data dataclass (`static_dir` / `modules` / `css` / `capability`) shared by
agent and host features, so feature packages never need to import Sovereign or
carry a fallback copy just to describe their console assets.

## Application extensions

Application packages can customize agent prompt context through the SDK-owned
`AppExtension` contract without importing the Sovereign runtime:

```python
from kestrel_sdk import AppExtension


class CompanionExtension(AppExtension):
    def get_system_prompt_prefix(self) -> str:
        return "You are this application's companion persona."
```

Sovereign consumes this contract and keeps a compatibility re-export at its
historic import path.

## Database surface (entity feature packages)

Feature packages that need raw SQL or ORM access (e.g. `kestrel-feature-entities`)
develop against `kestrel_sdk.storage.database`:

```python
from kestrel_sdk.storage.database import (
    DatabaseBackend,  # async ABC: execute / fetch_* / transaction
    PrivacyMode,  # 6-mode enum
    EngineTarget,  # frozen dataclass: url, persistent, description
    resolve_engine_target,  # PrivacyMode + fallback_url -> EngineTarget
)

target = resolve_engine_target(PrivacyMode.NORMAL, "postgresql+asyncpg://...")
# target.url is the SQLAlchemy URL the feature should bind its ORM engine to.
# Volatile modes (EPHEMERAL/ISOLATED) ignore fallback_url and return
# in-memory or tempfile sqlite URLs with persistent=False.
```

To get the **active** `DatabaseBackend` instance at runtime, features access it
through the agent context they already receive in their `Feature.__init__`:

```python
class MyEntityFeature(Feature):
    def __init__(self, agent):
        super().__init__(agent)
        self.db: DatabaseBackend = agent.db  # provided by sovereign
```

The SDK declares the `DatabaseBackend` ABC; sovereign provides the concrete
`SQLiteBackend` / `PostgresBackend` instance via `agent.db`. Feature packages
should never instantiate their own backend — that creates a parallel
connection pool and bypasses the agent's privacy enforcement.

## Isolated-feature configuration transitions

An isolated service can opt into a host-only configuration lifecycle request
when it needs to clean up resources using its **old** effective config before a
replacement (for example, retiring a Telegram webhook with the old token).
This is capability-negotiated: older services advertise no
`config_transition` capability, and hosts must use their existing replacement
flow without sending a transition RPC.

```python
from kestrel_sdk.isolated_feature import (
    ConfigTransitionResult,
    IsolatedFeatureService,
)


class TelegramService(IsolatedFeatureService):
    def __init__(self):
        super().__init__(name="telegram", version="1.0.0")
        self.advertise_config_transition()

    async def on_config_transition(self, next_config):
        # self.host_config is still the old effective config here.
        await self.retire_webhook(token=self.host_config["token"])
        # The host must now stop and replace this process with next_config.
        return ConfigTransitionResult.restart_required()
```

The host checks `client.supports_config_transition` and calls
`await client.prepare_config_transition(next_config)`. A `restart` result means
the hook completed ordered cleanup and the host must replace the process. A
service that can atomically switch its own resources may opt in with
`advertise_config_transition(supports_live_apply=True)` and return
`ConfigTransitionResult.applied()`; only then does the SDK update
`service.host_config` to the next config and the host may retain the process.
Failures raise `ConfigTransitionError` and leave the old config active.

If a caller cancels or times out a transition after it has started, the SDK
does not attempt to cancel the child hook: the request may already be on the
wire. It re-raises the cancellation locally and fences the client for process
replacement, so the host must stop and start the child with its known next
config rather than issue more tools or transitions against an unknown outcome.
`SubprocessIsolatedFeatureClient` retains that next config before releasing the
cancelled call, so its following `stop()` / `start()` replacement initializes
the new child with the intended effective config. A normal hook failure leaves
the old config retained because the existing child remains the known-safe
instance. Its process lifecycle, transition, and health calls are serialized
to keep a probe from spanning that state change. `stop()` is the exception:
it cancels an in-flight startup, health probe, or transition before taking its
bounded shutdown/terminate path, so a wedged child cannot block replacement.
Likewise, child/transport failures are reported as the generic typed
`ConfigTransitionError`; no transport or configuration detail is reflected in
the public lifecycle message.

The JSON-RPC method is `lifecycle/config-transition`, not a `tools/*` method,
so it is never agent-callable. The client serializes public transition and
shutdown calls: a transition already under way completes or fails before a
queued shutdown starts, while a transition begun after shutdown fails locally.
The service also processes transition and shutdown requests in stream order;
health requests queued behind a transition see its final state. Config values
are not logged or reflected in lifecycle error envelopes.

## Private host ingress

An isolated service can register bounded JSON callbacks for its trusted host
without creating agent-callable tools. Registration advertises the versioned
`host_ingress` capability during `initialize`; the host can call only the
registered names with `client.call_host_ingress(...)`. Legacy or malformed
capabilities, unknown names, malformed JSON, and oversized payloads fail
closed before the host writes the request.

```python
from kestrel_sdk.isolated_feature import IsolatedFeatureService


class ChannelService(IsolatedFeatureService):
    def __init__(self):
        super().__init__(name="channel", version="1.0.0")
        self.register_host_ingress("routing-update", self.apply_routing_update)

    async def apply_routing_update(self, payload):
        # payload is a strict JSON value, bounded to 64 KiB on both sides.
        self.routes = payload["routes"]
        return {"accepted": True}


# Host side, after initialize:
await client.call_host_ingress("routing-update", {"routes": ["primary"]})
```

Ingress uses the private `host/ingress` JSON-RPC method, never `tools/*`, so
it remains absent from `tools/list` and agent tool inventories. Synchronous
handlers run in a worker thread; native async handlers preserve normal request
concurrency. Ingress is rejected once shutdown or restart-required lifecycle
fencing begins, and public errors never reflect handler exceptions or payload
values.

## Idle retirement and inbound producers

An isolated service that may be retired by an idle-runtime policy must declare
whether it owns an unmanaged inbound producer. A producer polls, listens, or
otherwise receives work without a host tool call or private host-ingress call.

```python
from kestrel_sdk.isolated_feature import IsolatedFeatureService

service = IsolatedFeatureService(name="utility", version="1.0.0")
service.advertise_inbound_producer(False)  # Safe for host idle retirement.

# Host side, after initialize. This is true only for an exact False declaration.
assert client.idle_retirement_is_declared_safe
```

Use `True` for services with an independent poller or listener. Services that
omit the declaration remain ambiguous so compatible hosts keep them resident
unless an operator explicitly opts that named feature into retirement. Hosts
can inspect `client.inbound_producer_declaration`, which is `None` for missing
or malformed child metadata and therefore never authorizes retirement.

Declare producer ownership before `initialize`. If it derives from host config,
call `advertise_inbound_producer()` from the service's `configure()` hook; that
hook runs inside the initialize handshake before capabilities are assembled.
The value is frozen after the successful handshake because the host caches
negotiated capabilities. If a configuration transition would change whether
the service owns a producer, return `ConfigTransitionResult.restart_required()`
so the replacement child re-advertises its current value. Do not live-apply
that transition.

## Isolated tool execution context

Hosts can attach trusted, versioned invocation metadata to an isolated
`tools/call` without adding scheduler fields to user tool arguments. New SDK
services advertise the `tool_execution_context` capability; a host that passes
context fails closed against legacy services that do not advertise it.

```python
from kestrel_sdk.isolated_feature import (
    ToolExecutionContext,
    ToolExecutionTrigger,
    get_tool_execution_context,
)

# Host side: retain this idempotency key across retry attempts.
context = ToolExecutionContext(
    invocation_id="occurrence-execution-123",
    idempotency_key="payment-effect-123",
    attempt=2,
    trigger=ToolExecutionTrigger(
        kind="scheduler",
        id="occurrence-123",
        source_id="daily-payment-job",
    ),
)
await client.call_tool("charge", {"amount": 100}, context=context)


# Isolated handler side: this is task-local and never merged into arguments.
async def charge(arguments):
    context = get_tool_execution_context()
    if context is not None:
        await effect_boundary.deduplicate(context.idempotency_key)
```

The context schema has no free-form metadata field: it accepts only bounded
invocation, idempotency, retry, trigger identifiers, and timezone-aware trigger
timestamps. The service clears it after every successful, failed, or cancelled
invocation; `asyncio.to_thread` sync handlers receive the same active context.

## Channels, Delivery, And Output Contracts

Channel and delivery packages use SDK contracts rather than importing from the
full framework:

```python
from kestrel_sdk.channels import ChannelAdapter, ChannelMessage
from kestrel_sdk.delivery import DeliveryProvider, DeliveryTask, DeliveryResult
from kestrel_sdk.outputs import OutputEvent, OutputKind
```

Feature packages register concrete channel adapters through:

```toml
[project.entry-points."kestrel_sovereign.channel_adapters"]
telegram = "kestrel_channel_telegram:TelegramAdapter"
```

Delivery providers register through:

```toml
[project.entry-points."kestrel_sovereign.delivery_providers"]
sendgrid = "kestrel_delivery_sendgrid:SendGridDeliveryProvider"
```

The SDK owns only the public contracts. The framework owns runtime privacy
checks, signal dispatch, durable queues, and server composition.

## Timeline Protocols

Timeline implementations (e.g., story archive, health timelines) use SDK protocols for cross-package interoperability. The SDK provides three core protocols: `TimelineProtocol` defines the minimal shape any timeline must conform to, `TimelineSharingProtocol` enables pluggable serialization formats (JSON, FHIR, IPFS), and `VectorSearchBackend` abstracts semantic search across different vector stores (pgvector, pure-Python cosine).

### Implementing TimelineProtocol

Any class with the required attributes can serve as a timeline:

```python
from datetime import datetime


class StoryTimeline:
    def __init__(self):
        self.id = "timeline-123"
        self.agent_did = "did:key:abc"
        self.subject_name = "Jane Doe"
        self.title = "Jane's Life Story"
        self.coherence_score = 0.95
        self.created_at = datetime.now()
```

### Sharing and Serialization

Use `JSONTimelineSerializer` for default JSON output, or implement `TimelineSharingProtocol` for custom formats:

```python
from kestrel_sdk.timeline import JSONTimelineSerializer, TimelineSharingProtocol
import json

# Default JSON sharing
serializer = JSONTimelineSerializer()
data = serializer.serialize(timeline, events, people)


# Custom FHIR serializer
class FHIRTimelineSerializer:
    content_type = "application/fhir+json"

    def serialize(self, timeline, events, people) -> bytes:
        # Convert to FHIR Bundle format
        bundle = {"resourceType": "Bundle", "entry": [...]}
        return json.dumps(bundle).encode("utf-8")
```

### Vector Search

Implement `VectorSearchBackend` for semantic timeline search. The SDK ships two reference implementations in [`kestrel-feature-story-archive`](https://github.com/KestrelSovereignAI/kestrel-feature-story-archive): `PgVectorBackend` (PostgreSQL with pgvector extension) and `PurePythonBackend` (SQLite with cosine similarity).

```python
from kestrel_sdk.timeline import VectorSearchBackend


class MyVectorBackend:
    async def knn(self, query_embedding: bytes, k: int, filter: dict | None = None):
        # Return k-nearest neighbors by cosine similarity
        return [("event-5", 0.95), ("event-12", 0.89)]

    @property
    def supports_filters(self) -> bool:
        return True  # Can filter by timeline_id at query time
```

For a full timeline implementation with persistence, embeddings, and IPFS export, see [`kestrel-feature-story-archive`](https://github.com/KestrelSovereignAI/kestrel-feature-story-archive).

## Configuration

No environment variables required. This is a development-time dependency only.

## Development

```bash
uv pip install kestrel-sovereign-sdk && uv pip install -e .
uv run pytest
```
