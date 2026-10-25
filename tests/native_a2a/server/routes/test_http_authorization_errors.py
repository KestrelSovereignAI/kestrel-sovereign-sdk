# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.
"""HTTP authorization errors remain HTTP errors on both native bindings."""

from collections.abc import AsyncGenerator
from typing import NoReturn

import httpx
import pytest

from kestrel_sdk.native_a2a.utils.errors import TaskNotFoundError
from kestrel_sdk.native_a2a.server.context import ServerCallContext
from kestrel_sdk.native_a2a.server.events.event_queue import Event
from kestrel_sdk.native_a2a.server.request_handlers import RequestHandler
from kestrel_sdk.native_a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from kestrel_sdk.native_a2a.server.routes.rest_routes import create_rest_routes
from google.protobuf.message import Message as ProtoMessage
from starlette.applications import Starlette
from starlette.exceptions import HTTPException


class RejectingHandler(RequestHandler):
    """Reject at the actual handler boundary, before producing any event."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    async def _reject(
        self, params: ProtoMessage, context: ServerCallContext
    ) -> NoReturn:
        raise HTTPException(
            status_code=self.status_code,
            detail='Recipient policy refused this principal',
            headers={'WWW-Authenticate': 'Bearer realm="a2a"'},
        )

    on_get_task = _reject
    on_list_tasks = _reject
    on_cancel_task = _reject
    on_message_send = _reject
    on_create_task_push_notification_config = _reject
    on_get_task_push_notification_config = _reject
    on_list_task_push_notification_configs = _reject
    on_delete_task_push_notification_config = _reject
    on_get_extended_agent_card = _reject

    async def _reject_stream(
        self, params: ProtoMessage, context: ServerCallContext
    ) -> AsyncGenerator[Event]:
        await self._reject(params, context)
        yield  # pragma: no cover - declares the handler's async-generator contract

    on_message_send_stream = _reject_stream
    on_subscribe_to_task = _reject_stream


OPERATIONS = [
    ('GET', '/tasks/task', 'GetTask', {'id': 'task'}),
    ('GET', '/tasks', 'ListTasks', {}),
    ('POST', '/tasks/task:cancel', 'CancelTask', {'id': 'task'}),
    ('POST', '/message:send', 'SendMessage', {}),
    ('POST', '/message:stream', 'SendStreamingMessage', {}),
    ('POST', '/tasks/task:subscribe', 'SubscribeToTask', {'id': 'task'}),
    (
        'POST',
        '/tasks/task/pushNotificationConfigs',
        'CreateTaskPushNotificationConfig',
        {'taskId': 'task', 'id': 'push', 'url': 'https://callback.test/push'},
    ),
    (
        'GET',
        '/tasks/task/pushNotificationConfigs/push',
        'GetTaskPushNotificationConfig',
        {'taskId': 'task', 'id': 'push'},
    ),
    (
        'GET',
        '/tasks/task/pushNotificationConfigs',
        'ListTaskPushNotificationConfigs',
        {'taskId': 'task'},
    ),
    (
        'DELETE',
        '/tasks/task/pushNotificationConfigs/push',
        'DeleteTaskPushNotificationConfig',
        {'taskId': 'task', 'id': 'push'},
    ),
    ('GET', '/extendedAgentCard', 'GetExtendedAgentCard', {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('status_code', [401, 403])
@pytest.mark.parametrize('tenant', ['', 'tenant-one'])
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize('method,path,operation,params', OPERATIONS)
async def test_native_routes_preserve_http_authorization_errors(
    status_code: int,
    tenant: str,
    binding: str,
    method: str,
    path: str,
    operation: str,
    params: dict,
) -> None:
    handler = RejectingHandler(status_code)
    routes = [
        *create_jsonrpc_routes(handler, rpc_url='/rpc'),
        *create_rest_routes(handler),
    ]
    body = dict(params)
    if operation in {'SendMessage', 'SendStreamingMessage'}:
        body['message'] = {
            'messageId': 'request',
            'role': 'ROLE_USER',
            'parts': [{'text': 'request'}],
        }
    if tenant:
        body['tenant'] = tenant
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(Starlette(routes=routes)),
        base_url='http://authorization.test',
        headers={'A2A-Version': '1.0'},
    ) as client:
        if binding == 'JSONRPC':
            response = await client.post(
                '/rpc',
                json={
                    'jsonrpc': '2.0',
                    'id': 'request',
                    'method': operation,
                    'params': body,
                },
            )
        else:
            route = (f'/{tenant}' if tenant else '') + path
            response = await client.request(
                method,
                route,
                json=body if method == 'POST' else None,
            )
    assert response.status_code == status_code
    assert response.headers['www-authenticate'] == 'Bearer realm="a2a"'
    assert response.text == 'Recipient policy refused this principal'
    assert 'text/event-stream' not in response.headers['content-type']


class ProtocolRejectingHandler(RejectingHandler):
    """Raise a native protocol error at the same real route boundary."""

    async def _reject(
        self, params: ProtoMessage, context: ServerCallContext
    ) -> NoReturn:
        raise TaskNotFoundError('The requested task is absent')

    on_get_task = _reject
    on_list_tasks = _reject
    on_cancel_task = _reject
    on_message_send = _reject
    on_create_task_push_notification_config = _reject
    on_get_task_push_notification_config = _reject
    on_list_task_push_notification_configs = _reject
    on_delete_task_push_notification_config = _reject
    on_get_extended_agent_card = _reject


@pytest.mark.asyncio
@pytest.mark.parametrize('tenant', ['', 'tenant-one'])
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize('method,path,operation,params', OPERATIONS)
async def test_native_protocol_errors_use_their_binding_media_type(
    tenant: str, binding: str, method: str, path: str,
    operation: str, params: dict,
) -> None:
    handler = ProtocolRejectingHandler(404)
    app = Starlette(routes=[*create_jsonrpc_routes(handler, rpc_url='/rpc'),
                            *create_rest_routes(handler)])
    body = dict(params)
    if operation in {'SendMessage', 'SendStreamingMessage'}:
        body['message'] = {'messageId': 'request', 'role': 'ROLE_USER',
                           'parts': [{'text': 'request'}]}
    if tenant:
        body['tenant'] = tenant
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                 base_url='http://protocol.test',
                                 headers={'A2A-Version': '1.0'}) as client:
        if binding == 'JSONRPC':
            response = await client.post('/rpc', json={
                'jsonrpc': '2.0', 'id': 'request', 'method': operation, 'params': body,
            })
        else:
            response = await client.request(method, (f'/{tenant}' if tenant else '') + path,
                                            json=body if method == 'POST' else None)
    if binding == 'JSONRPC':
        assert response.status_code == 200
        assert response.headers['content-type'] == 'application/json'
        assert response.json()['error']['code'] == -32001
        assert response.json()['id'] == 'request'
    else:
        assert response.status_code == 404
        assert response.headers['content-type'] == 'application/a2a+json'
        error = response.json()['error']
        assert error['code'] == 404 and error['status'] == 'NOT_FOUND'
        assert error['details'][0]['@type'] == 'type.googleapis.com/google.rpc.ErrorInfo'
        assert error['details'][0]['reason'] == 'TASK_NOT_FOUND'
        assert error['details'][0]['domain'] == 'a2a-protocol.org'
    assert 'www-authenticate' not in response.headers
