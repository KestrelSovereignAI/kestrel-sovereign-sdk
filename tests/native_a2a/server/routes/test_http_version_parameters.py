# Modified by Kestrel: isolated SDK namespace; see VENDORED.json.
"""Exercise version carriers through real HTTP routes and task storage."""

import logging

import httpx
import pytest

from kestrel_sdk.native_a2a.server.agent_execution import AgentExecutor, RequestContext
from kestrel_sdk.native_a2a.server.context import ServerCallContext
from kestrel_sdk.native_a2a.server.events import EventQueue
from kestrel_sdk.native_a2a.server.request_handlers import DefaultRequestHandler
from kestrel_sdk.native_a2a.server.routes import create_jsonrpc_routes, create_rest_routes
from kestrel_sdk.native_a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from kestrel_sdk.native_a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
)
from kestrel_sdk.native_a2a.utils import error_handlers, version_validator
from starlette.applications import Starlette


class ObservedExecutor(AgentExecutor):
    def __init__(self) -> None:
        self.executions = 0

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        self.executions += 1
        await event_queue.enqueue_event(
            Message(
                message_id='version-carrier-response',
                role=Role.ROLE_AGENT,
                parts=[Part(text='echo-result')],
            )
        )

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        raise AssertionError('GetTask must not cancel agent work')


class ObservedTaskStore(InMemoryTaskStore):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def get(
        self, task_id: str, context: ServerCallContext
    ) -> Task | None:
        self.reads += 1
        return await super().get(task_id, context)


def create_app(executor: AgentExecutor, store: InMemoryTaskStore) -> Starlette:
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=store,
        agent_card=AgentCard(
            name='Version carriers',
            description='Exercise native HTTP service parameters',
            version='1',
            capabilities=AgentCapabilities(streaming=True),
            supported_interfaces=[
                AgentInterface(
                    url='http://test/rest',
                    protocol_binding='HTTP+JSON',
                    protocol_version='1.0',
                ),
                AgentInterface(
                    url='http://test/rpc',
                    protocol_binding='JSONRPC',
                    protocol_version='1.0',
                ),
            ],
            default_input_modes=['text/plain'],
            default_output_modes=['text/plain'],
            skills=[
                AgentSkill(
                    id='version-carriers',
                    name='Version carriers',
                    description='Echo with native version negotiation',
                    tags=['test'],
                )
            ],
        ),
    )
    return Starlette(
        routes=[
            *create_rest_routes(request_handler=handler, path_prefix='/rest'),
            *create_jsonrpc_routes(request_handler=handler, rpc_url='/rpc'),
        ]
    )


async def get_task(
    client: httpx.AsyncClient,
    binding: str,
    task_id: str,
    headers: list[tuple[str, str]],
    query: list[tuple[str, str]],
) -> httpx.Response:
    if binding == 'rest':
        return await client.get(
            '/rest/tasks/' + task_id, headers=headers, params=query
        )
    return await client.post(
        '/rpc',
        json={
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'GetTask',
            'params': {'id': task_id},
        },
        headers=headers,
        params=query,
    )


async def send_streaming_message(
    client: httpx.AsyncClient, binding: str, query: list[tuple[str, str]]
) -> httpx.Response:
    params = {
        'message': {
            'messageId': 'version-carrier-request',
            'role': 'ROLE_USER',
            'parts': [{'text': 'echo'}],
        }
    }
    return await client.post(
        '/rest/message:stream' if binding == 'rest' else '/rpc',
        json=params
        if binding == 'rest'
        else {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'SendStreamingMessage',
            'params': params,
        },
        params=query,
        headers={'Accept': 'text/event-stream'},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['rest', 'jsonrpc'])
@pytest.mark.parametrize(
    ('headers', 'query', 'accepted'),
    [
        ([], [('A2A-Version', '1.0')], True),
        ([], [('a2a-version', '1.0.999')], True),
        ([], [('a2A-vErSiOn', '1.0.1')], True),
        ([('a2A-vErSiOn', '1.0')], [], True),
        ([('A2A-Version', '1.0')], [('a2a-version', '1.0.1')], True),
        ([], [('A2A-Version', '1.0'), ('a2a-version', '1.0.2')], True),
        ([], [], False),
        ([], [('A2A-Version', '')], False),
        ([], [('A2A-Version', '1.1')], False),
        ([], [('A2A-Version', '2.0')], False),
        ([], [('A2A-Version', 'invalid')], False),
        ([('A2A-Version', '1.0')], [('A2A-Version', '1.1')], False),
        ([('A2A-Version', '1.1')], [('A2A-Version', '1.0')], False),
        ([('A2A-Version', '')], [('A2A-Version', '1.0')], False),
        ([], [('A2A-Version', '1.1'), ('A2A-Version', '1.0')], False),
        ([], [('A2A-Version', '1.0'), ('a2a-version', '1.1')], False),
        ([], [('A2A-Version', ''), ('a2a-version', '1.0')], False),
        ([('A2A-Version', '1.0'), ('a2a-version', '1.0.2')], [], True),
        ([('A2A-Version', '1.1'), ('A2A-Version', '1.0')], [], False),
        ([('A2A-Version', '1.0'), ('A2A-Version', '1.1')], [], False),
        ([('A2A-Version', ''), ('A2A-Version', '1.0')], [], False),
        ([], [('a2a-verſion', '1.0')], False),
    ],
)
async def test_http_version_carriers_before_task_read(
    binding: str,
    headers: list[tuple[str, str]],
    query: list[tuple[str, str]],
    accepted: bool,
) -> None:
    store = ObservedTaskStore()
    task = Task(
        id='version-carrier-task',
        context_id='version-carrier-context',
        status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
    )
    await store.save(task, ServerCallContext())
    executor = ObservedExecutor()
    app = create_app(executor, store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url='http://test'
    ) as client:
        response = await get_task(client, binding, task.id, headers, query)
    payload = response.json()
    assert executor.executions == 0
    if accepted:
        assert response.status_code == 200, payload
        result = payload if binding == 'rest' else payload['result']
        assert result['id'] == task.id
        assert store.reads == 1
    else:
        if binding == 'rest':
            assert response.status_code == 400, payload
        else:
            assert payload['error']['code'] == -32009, payload
        assert store.reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['rest', 'jsonrpc'])
@pytest.mark.parametrize('version', ['1.0', '1.0.999', '', '1.1', 'invalid'])
async def test_query_version_before_streaming_execution(
    binding: str, version: str
) -> None:
    executor = ObservedExecutor()
    store = ObservedTaskStore()
    app = create_app(executor, store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url='http://test'
    ) as client:
        response = await send_streaming_message(
            client, binding, [('a2A-vErSiOn', version)]
        )
    if version.startswith('1.0'):
        assert response.status_code == 200, response.text
        assert response.headers['content-type'].startswith('text/event-stream')
        assert 'echo-result' in response.text
        assert executor.executions == 1
    else:
        if binding == 'rest':
            assert response.status_code == 400, response.text
        else:
            assert response.json()['error']['code'] == -32009, response.text
        assert executor.executions == 0
        assert store.reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['rest', 'jsonrpc'])
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('control', ['\r\n', '\r', '\n', '\t'])
async def test_version_query_rejection_escapes_error_and_warning_logs(
    binding: str,
    streaming: bool,
    control: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    version = '1.1' + control + 'CRITICAL forged.auth accepted'
    executor = ObservedExecutor()
    store = ObservedTaskStore()
    caplog.set_level(logging.WARNING)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(executor, store)),
        base_url='http://test',
    ) as client:
        query = [('A2A-Version', version)]
        response = (
            await send_streaming_message(client, binding, query)
            if streaming
            else await get_task(client, binding, 'missing', [], query)
        )
    error = response.json()['error']
    if binding == 'rest':
        assert response.status_code == 400
    else:
        assert error['code'] == -32009
    assert repr(version) in error['message']
    assert not any(character in error['message'] for character in '\r\n\t')
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name in {error_handlers.__name__, version_validator.__name__}
    ]
    assert warnings
    assert all(
        not any(character in warning for character in '\r\n\t')
        for warning in warnings
    )
    assert executor.executions == store.reads == 0
