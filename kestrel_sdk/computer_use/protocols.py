"""Provisioning and interaction are independent plugin boundaries."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from kestrel_sdk.resources import Identifier, ResourceScope
from .models import (
    ActionReceipt, ActionRequest, ComputerSession, ControlRequest,
    DriverConnection, EnvironmentCapabilities, EnvironmentLease,
    EnvironmentQuote, EnvironmentRequest, ScreenObservation, SessionRequest,
)


@runtime_checkable
class EnvironmentProvider(Protocol):
    """Host-invoked provider; no authority is inferred from request fields.

The host persists request/quote/custody before acquire, authorizes its exact
quote, checks capabilities, cost, location and expiry, and reconciles unknown
outcomes by the same request ID. acquire must be idempotent for that request.
release is idempotent; a timeout is unknown, never evidence of physical release.
"""

    async def capabilities(self) -> EnvironmentCapabilities: ...
    async def quote(self, request: EnvironmentRequest) -> EnvironmentQuote: ...
    async def acquire(
        self, request: EnvironmentRequest, quote: EnvironmentQuote
    ) -> EnvironmentLease: ...
    async def inspect(
        self, scope: ResourceScope, lease: EnvironmentLease
    ) -> EnvironmentLease: ...
    async def release(
        self, scope: ResourceScope, lease: EnvironmentLease
    ) -> EnvironmentLease: ...
    async def connect(
        self, scope: ResourceScope, lease: EnvironmentLease
    ) -> DriverConnection: ...


@runtime_checkable
class ComputerDriver(Protocol):
    """An authenticated driver operates one admitted session, never provisions.

All calls enforce session identity, current control epoch and expiry at the
execution boundary. act rejects unsupported actions and stale observations;
reusing an action ID with different content is a conflict. Unknown actions
are reconciled with action_status, never automatically replayed. control fences
accepted work before takeover/handback and suspends agent observations. A lost
connection leaves custody with the host until physical retirement is proven.
Entry points return a zero-argument factory producing this protocol. open checks
connection.driver_id == self.driver_id, matching lease IDs, a live connection,
and request.expires_at <= connection.expires_at before sending any credential.
An action deadline is checked immediately before the effect, not only ingress.
"""

    driver_id: Identifier

    async def open(
        self, request: SessionRequest, connection: DriverConnection
    ) -> ComputerSession: ...
    async def status(
        self, scope: ResourceScope, session_id: str
    ) -> ComputerSession: ...
    async def observe(self, session: ComputerSession) -> ScreenObservation: ...
    async def act(
        self, session: ComputerSession, request: ActionRequest
    ) -> ActionReceipt: ...
    async def action_status(
        self, session: ComputerSession, action_id: str
    ) -> ActionReceipt: ...
    async def control(
        self, session: ComputerSession, request: ControlRequest
    ) -> ComputerSession: ...
