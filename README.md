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

## Database surface (entity feature packages)

Feature packages that need raw SQL or ORM access (e.g. `kestrel-feature-entities`)
develop against `kestrel_sdk.storage.database`:

```python
from kestrel_sdk.storage.database import (
    DatabaseBackend,           # async ABC: execute / fetch_* / transaction
    PrivacyMode,               # 5-mode enum
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

## Configuration

No environment variables required. This is a development-time dependency only.

## Development

```bash
uv pip install kestrel-sovereign-sdk && uv pip install -e .
uv run pytest
```
