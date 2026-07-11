# kestrel-sovereign-sdk

Lightweight SDK providing base interfaces, protocols, and utilities for Kestrel Sovereign feature package development. Feature packages depend on this SDK instead of the full framework, keeping dependencies minimal and development fast.

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
