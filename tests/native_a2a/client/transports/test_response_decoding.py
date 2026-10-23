# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.
"""Malformed peer data is typed without catching local transport callbacks."""

import json

from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest

from kestrel_sdk.native_a2a.client.errors import A2AClientError
from kestrel_sdk.native_a2a.client.transports.jsonrpc import JsonRpcTransport
from kestrel_sdk.native_a2a.client.transports.rest import RestTransport
from kestrel_sdk.native_a2a.helpers.proto_helpers import new_text_message
from kestrel_sdk.native_a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetExtendedAgentCardRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTasksRequest,
    Role,
    SendMessageRequest,
    SubscribeToTaskRequest,
    TaskPushNotificationConfig,
)
from google.protobuf.json_format import ParseError
from google.protobuf.message import Message


OPERATIONS = [
    ('get_task', GetTaskRequest(id='task')),
    ('list_tasks', ListTasksRequest()),
    ('cancel_task', CancelTaskRequest(id='task')),
    (
        'send_message',
        SendMessageRequest(
            message=new_text_message('Request', role=Role.ROLE_USER)
        ),
    ),
    (
        'send_message_streaming',
        SendMessageRequest(
            message=new_text_message('Request', role=Role.ROLE_USER)
        ),
    ),
    ('subscribe', SubscribeToTaskRequest(id='task')),
    (
        'create_task_push_notification_config',
        TaskPushNotificationConfig(
            task_id='task', id='push', url='https://callback.test/push'
        ),
    ),
    (
        'get_task_push_notification_config',
        GetTaskPushNotificationConfigRequest(task_id='task', id='push'),
    ),
    (
        'list_task_push_notification_configs',
        ListTaskPushNotificationConfigsRequest(task_id='task'),
    ),
    (
        'delete_task_push_notification_config',
        DeleteTaskPushNotificationConfigRequest(task_id='task', id='push'),
    ),
    ('get_extended_agent_card', GetExtendedAgentCardRequest()),
]
STREAMING = {'send_message_streaming', 'subscribe'}


async def invoke(transport: Any, method: str, params: Message) -> None:
    result = getattr(transport, method)(params)
    if method in STREAMING:
        stream: AsyncGenerator[Message, None] = result
        async for _ in stream:
            pass
    else:
        await result


def peer_response(
    request: httpx.Request, binding: str, method: str, fault: str
) -> httpx.Response:
    payload: Any = native_result(method)
    if fault == 'unknown-field':
        payload['unexpectedField'] = {'future': 'value'}
    elif fault == 'malformed-known-field':
        field = (
            'tasks'
            if method == 'list_tasks'
            else 'configs'
            if method == 'list_task_push_notification_configs'
            else 'name'
            if method == 'get_extended_agent_card'
            else 'id'
        )
        target = payload['task'] if 'task' in payload else payload
        target[field] = {'invalid': 'object'}
    if fault in {'nonfinite', 'overflow'}:
        payload = {
            'id': 'task',
            'contextId': 'context',
            'status': {'state': 'TASK_STATE_WORKING'},
            'metadata': {'invalid': float('nan')},
        }
    if binding == 'JSONRPC':
        payload = {
            'jsonrpc': '2.0',
            'id': json.loads(request.content)['id'],
            'result': payload,
        }
    if fault == 'root-array':
        payload = []
    elif fault == 'wrong-envelope' and binding == 'JSONRPC':
        payload = {'status': 'maintenance'}
    body = '{' if fault == 'invalid-json' else json.dumps(payload)
    if fault == 'overflow':
        body = body.replace('NaN', '1e10000')
    if method in STREAMING:
        return httpx.Response(
            200,
            headers={'content-type': 'text/event-stream'},
            text=f'data: {body}\n\n',
        )
    return httpx.Response(
        200, headers={'content-type': 'application/json'}, text=body
    )


def transport_for(http: httpx.AsyncClient, binding: str) -> Any:
    card = AgentCard(
        name='Peer',
        description='Native peer response contract',
        version='1.0',
        capabilities=AgentCapabilities(extended_agent_card=True),
        supported_interfaces=[
            AgentInterface(
                url='https://peer.test',
                protocol_binding=binding,
                protocol_version='1.0',
            )
        ],
        default_input_modes=['text/plain'],
        default_output_modes=['text/plain'],
        skills=[
            AgentSkill(
                id='chat',
                name='Chat',
                description='Receive messages',
                tags=['chat'],
            )
        ],
    )
    transport = JsonRpcTransport if binding == 'JSONRPC' else RestTransport
    return transport(http, card, 'https://peer.test')


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize('method,params', OPERATIONS)
@pytest.mark.parametrize('fault', ['root-array', 'invalid-json'])
async def test_malformed_json_is_typed_on_every_operation(
    binding: str, method: str, params: Message, fault: str
) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return peer_response(request, binding, method, fault)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(A2AClientError):
            await invoke(transport_for(http, binding), method, params)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize(
    'method,params',
    [
        operation
        for operation in OPERATIONS
        if operation[0] != 'delete_task_push_notification_config'
    ],
)
async def test_malformed_known_protobuf_response_field_is_typed(
    binding: str, method: str, params: Message
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return peer_response(request, binding, method, 'malformed-known-field')

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(A2AClientError):
            await invoke(transport_for(http, binding), method, params)


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize('fault', ['nonfinite', 'overflow'])
async def test_nonfinite_json_never_becomes_a_task(
    binding: str, fault: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return peer_response(request, binding, 'get_task', fault)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(A2AClientError):
            await invoke(
                transport_for(http, binding),
                'get_task',
                GetTaskRequest(id='task'),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize('method,params', OPERATIONS)
@pytest.mark.parametrize(
    'kind', ['value', 'type', 'json', 'protobuf', 'overflow', 'recursion']
)
async def test_local_admission_exception_is_preserved_before_response_decoding(
    binding: str, method: str, params: Message, kind: str
) -> None:
    errors = {
        'value': ValueError('Local admission refused'),
        'type': TypeError('Local admission refused'),
        'json': json.JSONDecodeError('Local admission refused', '', 0),
        'protobuf': ParseError('Local admission refused'),
        'overflow': OverflowError('Local admission refused'),
        'recursion': RecursionError('Local admission refused'),
    }
    failure = errors[kind]

    async def deny(request: httpx.Request) -> None:
        raise failure

    def must_not_send(request: httpx.Request) -> httpx.Response:
        raise AssertionError('Local admission must precede the peer request')

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(must_not_send),
        event_hooks={'request': [deny]},
    ) as http:
        with pytest.raises(type(failure)) as caught:
            await invoke(transport_for(http, binding), method, params)
    assert caught.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
async def test_valid_data_strings_survive_response_decoding(
    binding: str,
) -> None:
    payload = {
        'id': 'task',
        'metadata': {'sample': 'NaN', 'constant': 'Infinity'},
    }

    def respond(request: httpx.Request) -> httpx.Response:
        body = payload
        if binding == 'JSONRPC':
            body = {
                'jsonrpc': '2.0',
                'id': json.loads(request.content)['id'],
                'result': payload,
            }
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        task = await transport_for(http, binding).get_task(
            GetTaskRequest(id='task')
        )
    assert task.metadata['sample'] == 'NaN'
    assert task.metadata['constant'] == 'Infinity'


@pytest.mark.asyncio
async def test_jsonrpc_invalid_envelope_is_typed() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return peer_response(request, 'JSONRPC', 'get_task', 'wrong-envelope')

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(A2AClientError):
            await transport_for(http, 'JSONRPC').get_task(
                GetTaskRequest(id='task')
            )


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
async def test_void_response_still_requires_standard_json(binding: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return peer_response(
            request,
            binding,
            'delete_task_push_notification_config',
            'nonfinite',
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(A2AClientError):
            await transport_for(
                http, binding
            ).delete_task_push_notification_config(
                DeleteTaskPushNotificationConfigRequest(
                    task_id='task', id='push'
                )
            )


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize('method', ['get_task', 'subscribe'])
@pytest.mark.parametrize(
    'fault', ['huge-integer', 'deep-json', 'duplicate-keys']
)
async def test_review_limits_and_duplicate_keys(
    binding: str, method: str, fault: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if fault == 'huge-integer':
            inner = (
                '{"id":"task","metadata":{"number":' + '1' + '0' * 400 + '}}'
            )
        elif fault == 'deep-json':
            inner = (
                '{"id":"task","metadata":{"nested":'
                + '[' * 1200
                + '0'
                + ']' * 1200
                + '}}'
            )
        else:
            inner = '{"id":"first-task","id":"other-task"}'
        if method == 'subscribe':
            inner = '{"task":' + inner + '}'
        if binding == 'JSONRPC':
            response_id = json.dumps(json.loads(request.content)['id'])
            inner = (
                '{"jsonrpc":"2.0","id":'
                + response_id
                + ',"result":'
                + inner
                + '}'
            )
        if method == 'subscribe':
            return httpx.Response(
                200,
                headers={'content-type': 'text/event-stream'},
                text='data: ' + inner + '\n\n',
            )
        return httpx.Response(200, text=inner)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(A2AClientError):
            await invoke(
                transport_for(http, binding),
                method,
                GetTaskRequest(id='task')
                if method == 'get_task'
                else SubscribeToTaskRequest(id='task'),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['get_task', 'subscribe'])
async def test_deep_rest_error_body_retains_the_http_error(method: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text='[' * 1200 + '0' + ']' * 1200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(A2AClientError, match='503'):
            await invoke(
                transport_for(http, 'HTTP+JSON'),
                method,
                GetTaskRequest(id='task')
                if method == 'get_task'
                else SubscribeToTaskRequest(id='task'),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', ['"upstream busy"', '["upstream busy"]'])
async def test_rest_sse_error_keeps_non_object_error_text(payload: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={'content-type': 'text/event-stream'},
            text='event: error\ndata: ' + payload + '\n\n',
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(A2AClientError, match='upstream busy'):
            await invoke(
                transport_for(http, 'HTTP+JSON'),
                'subscribe',
                SubscribeToTaskRequest(id='task'),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
async def test_response_decoding_uses_bytes_without_response_callbacks(
    binding: str,
) -> None:
    callbacks: list[str] = []

    class LocalResponse(httpx.Response):
        def json(self, **kwargs: Any) -> Any:
            callbacks.append('json')
            if binding == 'JSONRPC':
                return {
                    'jsonrpc': '2.0',
                    'id': json.loads(self.request.content)['id'],
                    'result': {'id': 'local-callback-result'},
                }
            return {'id': 'local-callback-result'}

    def respond(request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = {'id': 'peer-byte-result'}
        if binding == 'JSONRPC':
            payload = {
                'jsonrpc': '2.0',
                'id': json.loads(request.content)['id'],
                'result': payload,
            }
        return LocalResponse(200, content=json.dumps(payload).encode())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        result = await transport_for(http, binding).get_task(
            GetTaskRequest(id='task')
        )
    assert result.id == 'peer-byte-result'
    assert callbacks == [], (
        'Peer decoding executed response-object code instead of parsing its bytes'
    )


def native_result(method: str) -> dict[str, Any]:
    task = {
        'id': 'task',
        'contextId': 'context',
        'status': {'state': 'TASK_STATE_WORKING'},
    }
    if method in STREAMING or method == 'send_message':
        return {'task': task}
    if method in {'get_task', 'cancel_task'}:
        return task
    return {}


@pytest.mark.asyncio
@pytest.mark.parametrize('method,params', OPERATIONS)
@pytest.mark.parametrize(
    'fault',
    [
        'version',
        'missing-version',
        'id',
        'missing-id',
        'null-id',
        'null-error',
        'empty-error',
        'both',
        'error-code',
        'error-message',
    ],
)
async def test_jsonrpc_envelope_matches_its_request(
    method: str, params: Message, fault: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = {
            'jsonrpc': '2.0',
            'id': json.loads(request.content)['id'],
            'result': native_result(method),
        }
        if fault == 'version':
            payload['jsonrpc'] = '1.0'
        elif fault == 'missing-version':
            del payload['jsonrpc']
        elif fault == 'id':
            payload['id'] = 'another-request'
        elif fault == 'missing-id':
            del payload['id']
        elif fault == 'null-id':
            payload['id'] = None
        elif fault == 'both':
            payload['error'] = {
                'code': -32603,
                'message': 'Invalid mixed response',
            }
        else:
            del payload['result']
            payload['error'] = (
                None
                if fault == 'null-error'
                else {}
                if fault == 'empty-error'
                else {'code': True, 'message': 'Invalid boolean code'}
                if fault == 'error-code'
                else {'code': -32603}
            )
        body = json.dumps(payload)
        if method in STREAMING:
            return httpx.Response(
                200,
                headers={'content-type': 'text/event-stream'},
                text='data: ' + body + '\n\n',
            )
        return httpx.Response(200, text=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(
            A2AClientError, match='Invalid A2A response payload'
        ):
            await invoke(transport_for(http, 'JSONRPC'), method, params)


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize(
    'method,params',
    [
        operation
        for operation in OPERATIONS
        if operation[0] in STREAMING or operation[0] == 'send_message'
    ],
)
async def test_native_message_response_requires_one_payload(
    binding: str, method: str, params: Message
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = {}
        if binding == 'JSONRPC':
            payload = {
                'jsonrpc': '2.0',
                'id': json.loads(request.content)['id'],
                'result': payload,
            }
        body = json.dumps(payload)
        if method in STREAMING:
            return httpx.Response(
                200,
                headers={'content-type': 'text/event-stream'},
                text='data: ' + body + '\n\n',
            )
        return httpx.Response(200, text=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(
            A2AClientError, match='Invalid A2A response payload'
        ):
            await invoke(transport_for(http, binding), method, params)


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['HTTP+JSON'])
@pytest.mark.parametrize('result', [None, []])
async def test_rest_delete_requires_an_object_response(
    binding: str, result: Any
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = result
        if binding == 'JSONRPC':
            payload = {
                'jsonrpc': '2.0',
                'id': json.loads(request.content)['id'],
                'result': result,
            }
        return (
            httpx.Response(200, json=payload)
            if payload is not None
            else httpx.Response(200, text='null')
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(
            A2AClientError, match='Invalid A2A response payload'
        ):
            await transport_for(
                http, binding
            ).delete_task_push_notification_config(
                DeleteTaskPushNotificationConfigRequest(
                    task_id='task', id='push'
                )
            )


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [-32700, -32600])
@pytest.mark.parametrize('method', ['get_task', 'subscribe'])
@pytest.mark.parametrize('sse_event', ['message', 'error'])
async def test_jsonrpc_unidentified_error_retains_its_standard_error(
    code: int, method: str, sse_event: str
) -> None:
    from kestrel_sdk.native_a2a.utils.errors import (
        InvalidRequestError,
        JSONParseError as RPCParseError,
    )

    def respond(request: httpx.Request) -> httpx.Response:
        payload = {
            'jsonrpc': '2.0',
            'id': None,
            'error': {
                'code': code,
                'message': 'Request could not be identified',
            },
        }
        if method == 'subscribe':
            return httpx.Response(
                200,
                headers={'content-type': 'text/event-stream'},
                text='event: '
                + sse_event
                + '\ndata: '
                + json.dumps(payload)
                + '\n\n',
            )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(
            RPCParseError if code == -32700 else InvalidRequestError
        ):
            await invoke(
                transport_for(http, 'JSONRPC'),
                method,
                GetTaskRequest(id='task')
                if method == 'get_task'
                else SubscribeToTaskRequest(id='task'),
            )


@pytest.mark.asyncio
async def test_jsonrpc_integral_number_error_code_maps_to_its_standard_error() -> (
    None
):
    from kestrel_sdk.native_a2a.utils.errors import InvalidParamsError

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                'jsonrpc': '2.0',
                'id': json.loads(request.content)['id'],
                'error': {'code': -32602.0, 'message': 'Invalid parameters'},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(InvalidParamsError):
            await transport_for(http, 'JSONRPC').get_task(
                GetTaskRequest(id='task')
            )


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize('method,params', OPERATIONS)
async def test_unknown_protobuf_response_fields_are_ignored(
    binding: str, method: str, params: Message
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return peer_response(request, binding, method, 'unknown-field')

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        await invoke(transport_for(http, binding), method, params)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'confirmation', [None, {}, {'deleted': True}, [], True, 'deleted', 0]
)
async def test_jsonrpc_delete_accepts_implementation_specific_confirmation(
    confirmation: Any,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                'jsonrpc': '2.0',
                'id': json.loads(request.content)['id'],
                'result': confirmation,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        assert (
            await transport_for(
                http, 'JSONRPC'
            ).delete_task_push_notification_config(
                DeleteTaskPushNotificationConfigRequest(
                    task_id='task', id='push'
                )
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'method,params,mode',
    [(method, params, 'json') for method, params in OPERATIONS]
    + [
        (method, params, mode)
        for method, params in OPERATIONS
        if method in STREAMING
        for mode in ('message', 'error')
    ],
)
@pytest.mark.parametrize('code', [-32603, -32001, -32004])
@pytest.mark.parametrize('id_shape', ['null', 'absent'])
async def test_jsonrpc_unidentified_native_error_keeps_its_type(
    method: str,
    params: Message,
    mode: str,
    code: int,
    id_shape: str,
) -> None:
    from kestrel_sdk.native_a2a.utils.errors import (
        InternalError,
        TaskNotFoundError,
        UnsupportedOperationError,
    )

    expected = {
        -32603: InternalError,
        -32001: TaskNotFoundError,
        -32004: UnsupportedOperationError,
    }[code]

    def respond(request: httpx.Request) -> httpx.Response:
        payload = {
            'jsonrpc': '2.0',
            'id': None,
            'error': {
                'code': code,
                'message': 'Method failed',
                'data': [
                    {
                        '@type': 'type.googleapis.com/google.rpc.ErrorInfo',
                        'metadata': {'task': 'task'},
                    }
                ],
            },
        }
        if id_shape == 'absent':
            del payload['id']
        body = json.dumps(payload)
        return httpx.Response(
            200,
            text=body
            if mode == 'json'
            else 'event: ' + mode + '\ndata: ' + body + '\n\n',
            headers={
                'content-type': 'application/json'
                if mode == 'json'
                else 'text/event-stream'
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(expected, match='Method failed') as caught:
            await invoke(transport_for(http, 'JSONRPC'), method, params)
    assert caught.value.data == {'task': 'task'}


@pytest.mark.asyncio
@pytest.mark.parametrize('method,params', OPERATIONS)
@pytest.mark.parametrize(
    'fault',
    [
        'wrong-id',
        'boolean-id',
        'nonobject-error',
        'bad-code',
        'bad-message',
        'mixed-result',
    ],
)
async def test_null_id_error_acceptance_still_rejects_invalid_envelopes(
    method: str,
    params: Message,
    fault: str,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = {
            'jsonrpc': '2.0',
            'id': None,
            'error': {'code': -32001, 'message': 'Task not found'},
        }
        if fault == 'wrong-id':
            payload['id'] = 'another-request'
        elif fault == 'boolean-id':
            payload['id'] = True
        elif fault == 'nonobject-error':
            del payload['id']
            payload['error'] = None
        elif fault == 'bad-code':
            payload['error']['code'] = True
        elif fault == 'bad-message':
            payload['error']['message'] = None
        else:
            payload['result'] = native_result(method)
        body = json.dumps(payload)
        return httpx.Response(
            200,
            text='data: ' + body + '\n\n' if method in STREAMING else body,
            headers={
                'content-type': 'text/event-stream'
                if method in STREAMING
                else 'application/json'
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(
            A2AClientError, match='Invalid A2A response payload'
        ):
            await invoke(transport_for(http, 'JSONRPC'), method, params)


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize(
    'method,params',
    [operation for operation in OPERATIONS if operation[0] in STREAMING],
)
@pytest.mark.parametrize(
    'body', [b'{"bad":"\xff"}', ('[' * 1200 + ']' * 1200).encode()]
)
async def test_non_sse_stream_response_still_has_typed_peer_decoding(
    binding: str,
    method: str,
    params: Message,
    body: bytes,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={'content-type': 'application/json'}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(A2AClientError):
            await invoke(transport_for(http, binding), method, params)


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize(
    'method,params',
    [operation for operation in OPERATIONS if operation[0] in STREAMING],
)
@pytest.mark.parametrize(
    'error_type', [TypeError, OverflowError, RecursionError]
)
async def test_non_sse_local_body_read_keeps_its_original_error(
    binding: str,
    method: str,
    params: Message,
    error_type: type[Exception],
) -> None:
    original = error_type('Local body read failure')

    class LocalResponse(httpx.Response):
        async def aread(self) -> bytes:
            raise original

    def respond(request: httpx.Request) -> httpx.Response:
        return LocalResponse(
            200, content=b'{}', headers={'content-type': 'application/json'}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(error_type) as observed:
            await invoke(transport_for(http, binding), method, params)
    assert observed.value is original


def invalid_message_shape(method: str, value: Any) -> dict[str, Any]:
    if method == 'list_tasks':
        return {'tasks': [value]}
    if method in {'get_task', 'cancel_task'}:
        return {'id': 'task', 'status': value}
    if method in STREAMING:
        return {'statusUpdate': value}
    if method == 'send_message':
        return {'task': value}
    if method == 'list_task_push_notification_configs':
        return {'configs': [value]}
    if method == 'get_extended_agent_card':
        return {'capabilities': value}
    return {'authentication': value}


def native_wire_response(
    request: httpx.Request, binding: str, method: str, payload: Any
) -> httpx.Response:
    if binding == 'JSONRPC':
        payload = {
            'jsonrpc': '2.0',
            'id': json.loads(request.content)['id'],
            'result': payload,
        }
    body = json.dumps(payload)
    if method in STREAMING:
        return httpx.Response(
            200,
            headers={'content-type': 'text/event-stream'},
            text=f'data: {body}\n\n',
        )
    return httpx.Response(
        200, headers={'content-type': 'application/json'}, text=body
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize(
    'method,params',
    [
        item
        for item in OPERATIONS
        if item[0] != 'delete_task_push_notification_config'
    ],
)
@pytest.mark.parametrize('value', ['completed', [], ['unknown']])
async def test_non_object_message_fields_are_not_successful_empty_messages(
    binding: str, method: str, params: Message, value: Any
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return native_wire_response(
            request, binding, method, invalid_message_shape(method, value)
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(A2AClientError):
            await invoke(transport_for(http, binding), method, params)


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize(
    'method,params',
    [
        item
        for item in OPERATIONS
        if item[0] != 'delete_task_push_notification_config'
    ],
)
@pytest.mark.parametrize(
    'position', ['unknown-key', 'unknown-value', 'message', 'enum']
)
async def test_lone_surrogates_are_typed_before_entering_protobuf(
    binding: str, method: str, params: Message, position: str
) -> None:
    value = chr(0xD83D)
    if position == 'unknown-key':
        payload = {**native_result(method), value: 1}
    elif position == 'unknown-value':
        payload = {**native_result(method), 'futureField': value}
    elif position == 'message':
        payload = invalid_message_shape(method, value)
    else:
        task = {'id': 'task', 'status': {'state': value}}
        payload = (
            {'task': task}
            if method == 'send_message' or method in STREAMING
            else task
        )

    def respond(request: httpx.Request) -> httpx.Response:
        return native_wire_response(request, binding, method, payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(A2AClientError):
            await invoke(transport_for(http, binding), method, params)


@pytest.mark.parametrize(
    'value',
    [None, True, 42, 'plain', [1, {'nested': False}], {'nested': [None]}],
)
def test_native_json_well_known_representations_stay_valid(value: Any) -> None:
    from kestrel_sdk.native_a2a.client.transports.response_parsing import (
        load_json_response,
        parse_protobuf_response,
    )
    from kestrel_sdk.native_a2a.types import Task
    from google.protobuf.json_format import MessageToDict

    payload = {
        'id': 'task',
        'contextId': 'context',
        'status': {
            'state': 'TASK_STATE_COMPLETED',
            'timestamp': '2026-10-21T14:00:00Z',
        },
        'history': [
            {
                'messageId': 'reply',
                'role': 'ROLE_AGENT',
                'parts': [{'data': value}],
            }
        ],
        'metadata': {'unicode': '\U0001f600', 'value': value},
        'futureField': {'unknown': [1, 'forward-compatible']},
    }
    decoded = parse_protobuf_response(
        load_json_response(json.dumps(payload)), Task()
    )
    wire = MessageToDict(decoded)
    assert wire['history'][0]['parts'][0]['data'] == value
    assert wire['metadata'] == payload['metadata']
    assert wire['status']['timestamp'] == payload['status']['timestamp']


@pytest.mark.parametrize(
    'field', ['securitySchemes', 'securityRequirements', 'supportedInterfaces']
)
@pytest.mark.parametrize('value', ['invalid', []])
def test_descriptor_container_shapes_cover_maps_and_repeated_messages(
    field: str, value: Any
) -> None:
    from kestrel_sdk.native_a2a.client.transports.response_parsing import parse_protobuf_response
    from kestrel_sdk.native_a2a.types import AgentCard

    descriptor = AgentCard.DESCRIPTOR.fields_by_name.get(field)
    if descriptor is None:
        descriptor = next(
            f for f in AgentCard.DESCRIPTOR.fields if f.json_name == field
        )
    from typing import cast

    from kestrel_sdk.native_a2a.utils.proto_utils import _field_is_repeated
    from google.protobuf.descriptor import FieldDescriptor

    assert (
        _field_is_repeated(cast(FieldDescriptor, descriptor))
        and descriptor.message_type is not None
    )
    payload = {
        field: {'signedSender': value}
        if descriptor.message_type.GetOptions().map_entry
        else [value]
    }
    with pytest.raises(A2AClientError):
        parse_protobuf_response(payload, AgentCard())


@pytest.mark.parametrize(
    'position', ['unknown-key', 'enum', 'message', 'struct-key', 'struct-value']
)
def test_direct_protobuf_decoder_validates_unicode_without_json_loader(
    position: str,
) -> None:
    from kestrel_sdk.native_a2a.client.transports.response_parsing import parse_protobuf_response
    from kestrel_sdk.native_a2a.types import Task

    surrogate = chr(0xD83D)
    payload: dict[str, Any] = {'id': 'task'}
    if position == 'unknown-key':
        payload[surrogate] = 'future field'
    elif position == 'enum':
        payload['status'] = {'state': surrogate}
    elif position == 'message':
        payload['status'] = surrogate
    elif position == 'struct-key':
        payload['metadata'] = {surrogate: 1}
    else:
        payload['metadata'] = {'value': surrogate}
    with pytest.raises(A2AClientError):
        parse_protobuf_response(payload, Task())


@pytest.mark.asyncio
@pytest.mark.parametrize('binding', ['JSONRPC', 'HTTP+JSON'])
@pytest.mark.parametrize('method,params', OPERATIONS)
async def test_structured_errors_keep_their_type_with_escaped_surrogate_messages(
    binding: str, method: str, params: Message
) -> None:
    from kestrel_sdk.native_a2a.utils.errors import TaskNotFoundError

    message = 'Task not found: ' + chr(0xD83D)

    def respond(request: httpx.Request) -> httpx.Response:
        if binding == 'JSONRPC':
            payload = {
                'jsonrpc': '2.0',
                'id': json.loads(request.content)['id'],
                'error': {'code': -32001, 'message': message},
            }
            status = 200
        else:
            payload = {
                'error': {
                    'code': 404,
                    'status': 'NOT_FOUND',
                    'message': message,
                    'details': [
                        {
                            '@type': 'type.googleapis.com/google.rpc.ErrorInfo',
                            'reason': 'TASK_NOT_FOUND',
                            'domain': 'a2a-protocol.org',
                        }
                    ],
                }
            }
            status = 404
        body = json.dumps(payload)
        if method in STREAMING:
            return httpx.Response(
                200,
                headers={'content-type': 'text/event-stream'},
                text=f'event: error\ndata: {body}\n\n',
            )
        return httpx.Response(
            status, headers={'content-type': 'application/json'}, text=body
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(TaskNotFoundError) as caught:
            await invoke(transport_for(http, binding), method, params)
    assert message in str(caught.value)


@pytest.mark.asyncio
async def test_jsonrpc_delete_confirmation_has_no_protobuf_unicode_constraint() -> (
    None
):
    method, params = next(
        item
        for item in OPERATIONS
        if item[0] == 'delete_task_push_notification_config'
    )

    def respond(request: httpx.Request) -> httpx.Response:
        return native_wire_response(
            request, 'JSONRPC', method, {'future' + chr(0xD83D): chr(0xD83D)}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as http:
        await invoke(transport_for(http, 'JSONRPC'), method, params)


def test_native_decoder_uses_the_canonical_descriptor_cardinality_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kestrel_sdk.native_a2a.client.transports import response_parsing
    from kestrel_sdk.native_a2a.types import Task
    from google.protobuf.descriptor import FieldDescriptor

    original = response_parsing._field_is_repeated
    seen = []

    def observed(field: FieldDescriptor) -> bool:
        seen.append(field.full_name)
        return original(field)

    monkeypatch.setattr(response_parsing, '_field_is_repeated', observed)
    response_parsing.parse_protobuf_response(
        {'id': 'task', 'status': {'state': 'TASK_STATE_WORKING'}}, Task()
    )
    assert seen, 'Native decoder bypassed the canonical descriptor helper'
    assert Task.DESCRIPTOR.fields_by_name['id'].full_name in seen
