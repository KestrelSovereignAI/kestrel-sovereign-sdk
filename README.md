# kestrel-sovereign-sdk

Lightweight SDK providing base interfaces, protocols, and utilities for Kestrel Sovereign feature package development. Feature packages depend on this SDK instead of the full framework, keeping dependencies minimal and development fast.

## Voice provider contracts

`kestrel_sdk.voice` defines independent TTS, STT, and realtime conversation
provider contracts. Realtime providers declare capability metadata and mint a
provider-neutral browser bootstrap (WebRTC or WebSocket); voice IDs are scoped
to their provider. Tool-call batches pair every governed function result with
one continuation, while legacy single-result methods remain available for
older adapters.

## Installation

```bash
uv pip install git+https://github.com/KestrelSovereignAI/kestrel-sovereign-sdk.git
```

With encryption helpers:

```bash
uv pip install "kestrel-sovereign-sdk[crypto] @ git+https://github.com/KestrelSovereignAI/kestrel-sovereign-sdk.git"
```

## Dependencies

- `pydantic>=2.0`
- Optional: `cryptography>=42.0` (via `[crypto]` extra)

## Usage

```python
from kestrel_sdk.features.base import Feature, Tool

class MyFeature(Feature):
    name = "my-feature"

    def get_tools(self):
        return [Tool(name="my-tool", description="Does something", handler=self.handle)]
```

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
    name = "fleet-observability"       # stable slug for discovery / mounting
    capability = "fleet.observe"       # optional capability gate

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
    DatabaseBackend,           # async ABC: execute / fetch_* / transaction
    PrivacyMode,               # 6-mode enum
    EngineTarget,              # frozen dataclass: url, persistent, description
    resolve_engine_target,     # PrivacyMode + fallback_url -> EngineTarget
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
        self.db: DatabaseBackend = agent.db   # provided by sovereign
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
