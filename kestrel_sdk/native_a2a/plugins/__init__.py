# Modified by Kestrel: isolated SDK namespace; see VENDORED.json.
"""Plugins: hooks into the A2A client and `DefaultRequestHandlerV2`.

Example:
    from kestrel_sdk.native_a2a import plugins

    class TaskAudit(plugins.TaskObserverPlugin):
        name = 'task-audit'

        async def on_task_event(self, info: plugins.TaskEventInfo) -> None:
            print(info.task_id, info.from_state, '->', info.to_state)

    plugins.set_global(plugins.sequential(TaskAudit()))
    # Create DefaultRequestHandlerV2 and clients after this point.
"""

from kestrel_sdk.native_a2a.plugins.base import (
    ClientPlugin,
    CompletePlugin,
    ExecutionPlugin,
    Plugin,
    PushPlugin,
    ServerPlugin,
    TaskObserverPlugin,
)
from kestrel_sdk.native_a2a.plugins.manager import (
    PluginManager,
    SequentialPluginManager,
    get_global,
    sequential,
    set_global,
)
from kestrel_sdk.native_a2a.plugins.types import (
    ClientRequest,
    ClientResponse,
    PushRequest,
    PushResult,
    ServerRequest,
    ServerResponse,
    TaskEventInfo,
)


__all__ = [
    'ClientPlugin',
    'ClientRequest',
    'ClientResponse',
    'CompletePlugin',
    'ExecutionPlugin',
    'Plugin',
    'PluginManager',
    'PushPlugin',
    'PushRequest',
    'PushResult',
    'SequentialPluginManager',
    'ServerPlugin',
    'ServerRequest',
    'ServerResponse',
    'TaskEventInfo',
    'TaskObserverPlugin',
    'get_global',
    'sequential',
    'set_global',
]
