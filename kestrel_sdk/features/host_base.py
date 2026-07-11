"""Host-scoped feature contract — SDK interface.

``HostFeature`` is the host/fleet-scoped sibling of :class:`~kestrel_sdk.features.base.Feature`.

Where a ``Feature`` **is a subagent** — it is bound to a single agent
(``self.agent``), mounts its router under that agent's prefix behind a
``get_agent`` dependency, and is called as a tool with its own LLM context —
a ``HostFeature`` runs once at **host/fleet scope**. It has no agent binding
at all: its router mounts at the host root, its lifecycle is tied to host
start/stop rather than agent enable/disable, and any store it opens is a
**host backend** operated under a fleet tenant scope (layered on top of the
SDK's storage primitives by the feature layer, not here).

This contract is what ``kestrel-sovereign`` discovers and mounts, and what
host features (e.g. fleet observability in ``kestrel-claws``) implement.

Design principles (issue #46):

* **No subagent/agent binding.** Nothing on this contract references an
  agent, ``get_agent``, or an agent-scoped store. ``Feature`` is left
  untouched.
* **Dependency-free.** The SDK owns contracts and primitives only. The host
  store handle builds on the SDK's own storage layer
  (:class:`~kestrel_sdk.storage.database.DatabaseBackend` +
  :func:`~kestrel_sdk.storage.database.resolve_engine_target`) and returns an
  engine target / backend handle. ``kestrel-feature-entities`` and
  ``TenantContext`` are layered on top *by the feature layer*, never imported
  here.
* **Thin, well-documented ABC.** Concrete wiring (router mounting, backplane
  transport, config source) lives in the framework.

Feature packages import from here::

    from kestrel_sdk.features.host_base import HostFeature, HostContext
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
    Protocol,
    runtime_checkable,
)

from kestrel_sdk.features.ui import UIContributions
from kestrel_sdk.storage.database import (
    DatabaseBackend,
    EngineTarget,
    PrivacyMode,
    resolve_engine_target,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import APIRouter


@runtime_checkable
class HostContext(Protocol):
    """Runtime context handed to a :class:`HostFeature` at host scope.

    This is the surface every host feature programs against — the host-scoped
    analogue of the ``agent`` a :class:`~kestrel_sdk.features.base.Feature`
    receives, but typed and deliberately minimal. Sovereign provides the
    concrete implementation; host features depend only on this Protocol.

    It is ``runtime_checkable`` so tests and the framework can assert a
    provided object satisfies the shape, and extensible — new host-scoped
    handles are added here as later phases need them.

    Attributes / accessors:
        db: The active host :class:`~kestrel_sdk.storage.database.DatabaseBackend`.
            This is the fleet/host backend, never an agent-scoped store.
        backplane: A pub/sub backplane handle host features use to publish and
            subscribe to live streams (fleet events, observability feeds).
            Typed ``Any`` because the transport contract is owned elsewhere.
        config: Host configuration mapping (read-only from the feature's
            perspective).
    """

    @property
    def db(self) -> DatabaseBackend:
        """Active host database backend (fleet/host scope)."""
        ...

    @property
    def backplane(self) -> Any:
        """Pub/sub backplane handle for host-feature live streams."""
        ...

    @property
    def config(self) -> Any:
        """Host configuration (mapping-like, read-only)."""
        ...


class HostFeature(ABC):
    """Base class for host/fleet-scoped Kestrel features.

    A ``HostFeature`` runs once per host, not per agent. It is **not** a
    subagent: it has no ``agent`` binding, is never called as a tool, and its
    router mounts at the host root with no agent prefix and no ``get_agent``
    dependency.

    Contrast with :class:`~kestrel_sdk.features.base.Feature`:

    ==============  ==================  ================================
    aspect          Feature             HostFeature
    ==============  ==================  ================================
    scope           one subagent        host / fleet
    binding         ``self.agent``      none (``HostContext`` at runtime)
    router mount    under agent prefix  host root (no prefix)
    lifecycle       enable / disable    ``on_host_start`` / ``on_host_stop``
    store           agent store         host backend under fleet tenancy
    called as tool  yes (A2A)           no
    ==============  ==================  ================================

    Subclasses set :attr:`name` (a stable slug) and optionally
    :attr:`capability`, override :meth:`get_router` /
    :meth:`get_ui_contributions` to contribute a router and console panels,
    implement the :meth:`on_host_start` / :meth:`on_host_stop` lifecycle, and
    use :meth:`resolve_host_engine_target` to bind a host-scoped store.
    """

    #: Stable, host-unique slug for this feature (discovery / mounting / logs).
    #: Defaults to the class name; subclasses should set an explicit slug.
    name: str = "host-feature"

    #: Optional capability slug used to gate access to this host feature's
    #: router and UI. ``None`` means ungated.
    capability: Optional[str] = None

    # =========================================================================
    # Routing
    # =========================================================================

    def get_router(self) -> "Optional[APIRouter]":
        """Return a FastAPI ``APIRouter`` mounted at the **host root**, or ``None``.

        Unlike :meth:`Feature.get_router`, the returned router is mounted with
        **no agent prefix and no ``get_agent`` dependency** — it operates at
        host/fleet scope. Returning ``None`` means the feature contributes no
        HTTP surface.
        """
        return None

    # =========================================================================
    # Host lifecycle
    # =========================================================================

    async def on_host_start(self, ctx: HostContext) -> None:
        """Called once when the host starts.

        Use for host-scoped setup: open a store engine, start a pub/sub
        backplane subscription, spin up background tasks. ``ctx`` exposes the
        host store/backplane/config handles (see :class:`HostContext`).
        """
        pass

    async def on_host_stop(self, ctx: HostContext) -> None:
        """Called once when the host stops.

        Mirror of :meth:`on_host_start`: close store engines, stop backplane
        subscriptions, cancel background tasks. Should be idempotent and
        tolerate a partially-initialised state (start may have failed).
        """
        pass

    # =========================================================================
    # Host store handle
    # =========================================================================

    def resolve_host_engine_target(
        self,
        fallback_url: Optional[str],
        mode: PrivacyMode = PrivacyMode.NORMAL,
    ) -> EngineTarget:
        """Resolve the host-scoped :class:`EngineTarget` for this feature.

        Builds on the SDK's own storage layer
        (:func:`~kestrel_sdk.storage.database.resolve_engine_target`) rather
        than importing ``kestrel-feature-entities`` or ``TenantContext`` — the
        SDK stays dependency-free. The returned ``EngineTarget.url`` is the
        SQLAlchemy URL a host store should bind to; the **feature layer**
        (Phase 2) layers entities + a fleet ``TenantContext`` on top of this
        handle in its own code.

        This is deliberately **not** coupled to any agent store: it defaults
        to the persistent ``NORMAL`` mode and passes ``fallback_url`` (the
        host backend URL) straight through.

        Args:
            fallback_url: SQLAlchemy URL of the host backend. Required for
                persistent modes; ignored for volatile ones.
            mode: Privacy mode for the host store. Defaults to
                :attr:`PrivacyMode.NORMAL` (persistent).

        Returns:
            The resolved :class:`EngineTarget`.
        """
        return resolve_engine_target(mode, fallback_url)

    # =========================================================================
    # Host UI contributions
    # =========================================================================

    def get_ui_contributions(self) -> Optional[UIContributions]:
        """Return console panels this host feature contributes, or ``None``.

        Reuses the SDK-owned :class:`~kestrel_sdk.features.ui.UIContributions`
        shape (``static_dir`` + ``modules`` + ``capability`` + ``css``) so host
        features can ship console panels at host scope. Returning ``None``
        means the feature contributes no UI.
        """
        return None


__all__ = ["HostFeature", "HostContext"]
