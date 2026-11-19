"""Native action declarations seal schemas and keep identity out of params."""

from dataclasses import FrozenInstanceError, asdict, replace
import hashlib
import inspect
import json
from functools import partial

import pytest

from kestrel_sdk.hooks.base import HookEvent, HookInput, HookOutput
from kestrel_sdk.llm.response import ToolCall
from kestrel_sdk.tools import AgentTool, GovernedToolBinding, NativeAction, NativeActionContext, ToolCategory, ToolSchema


@pytest.fixture
def context():
    return NativeActionContext(action='cancel', method='tasks/cancel', task_handle='local-task',
        remote_task_digest=hashlib.sha256(b'private remote task ID').hexdigest(),
        owner='mcp:original', tool_name='original_tool', registration_id='original-batch',
        schema_digest=hashlib.sha256(b'original schema').hexdigest(),
        attribution_kind='service', attribution_digest=hashlib.sha256(b'original actor').hexdigest(),
        input_revision=3)


async def handler(context, params):
    return context.task_handle, params


def test_native_schema_is_sealed_and_each_reader_gets_its_own_values():
    raw = {'type': 'object', 'properties': {'inputResponses': {'type': 'object'}},
           'additionalProperties': False}
    declaration = NativeAction('update', 'tasks/update', json.dumps(raw).encode(), handler)
    digest = declaration.schema_digest
    raw['properties']['inputResponses']['type'] = 'string'
    first = declaration.schema
    first['properties']['inputResponses']['type'] = 'array'
    assert declaration.schema['properties']['inputResponses']['type'] == 'object'
    assert declaration.schema_digest == digest
    assert json.loads(declaration.schema_json)['properties']['inputResponses']['type'] == 'object'
    with pytest.raises(FrozenInstanceError):
        declaration.handler = None


def test_schema_identity_ignores_formatting_and_key_order():
    first = NativeAction('cancel', 'tasks/cancel', b'{ "type": "object", "properties": {} }', handler)
    second = NativeAction('cancel', 'tasks/cancel', b'{"properties":{},"type":"object"}', handler)
    assert first.schema_json == second.schema_json
    assert first.schema_digest == second.schema_digest


@pytest.mark.parametrize('raw', [pytest.param(b'[]', id='array'), pytest.param(b'null', id='null'),
    pytest.param(b'1', id='number'), pytest.param(b'{', id='invalid-json'),
    pytest.param(b'\xff', id='invalid-utf8'), pytest.param(b'{"minimum":NaN}', id='nonfinite-nan'),
    pytest.param(b'{"maximum":Infinity}', id='nonfinite-infinity')])
def test_non_object_nonfinite_or_invalid_schema_is_refused(raw):
    with pytest.raises((ValueError, UnicodeError)):
        NativeAction('cancel', 'tasks/cancel', raw, handler)


@pytest.mark.parametrize('raw', [pytest.param(None, id='none'), pytest.param({}, id='mapping'),
    pytest.param(bytearray(b'{}'), id='mutable-bytes'), pytest.param('{}', id='text')])
def test_schema_requires_immutable_bytes(raw):
    with pytest.raises(TypeError, match='immutable JSON bytes'):
        NativeAction('cancel', 'tasks/cancel', raw, handler)


class _SyncHandler:
    def __call__(self, context, params):
        raise AssertionError('Synchronous handler must never run')


class _AsyncHandler:
    def __init__(self, *args):
        if args:
            raise AssertionError('Handler constructor must never execute as an action')

    async def __call__(self, context, params):
        return await handler(context, params)


def _spoofed_sync_handler():
    target = _SyncHandler()
    target.__call__ = handler
    return target


class _AsyncConstructorMeta(type):
    async def __call__(cls, context, params):
        return None


class _AsyncConstructor(metaclass=_AsyncConstructorMeta):
    pass


@pytest.mark.parametrize('target', [None, lambda context, params: handler(context, params),
    _SyncHandler(), _AsyncHandler, partial(_AsyncHandler), _spoofed_sync_handler(), _AsyncConstructor],
    ids=['missing', 'sync-wrapper', 'sync-object', 'class', 'partial-class', 'instance-spoof', 'async-metaclass'])
def test_action_requires_its_registered_callable(target):
    with pytest.raises(TypeError, match='registered async handler'):
        NativeAction('cancel', 'tasks/cancel', b'{}', target)


@pytest.mark.parametrize('field', ['name', 'method'])
@pytest.mark.parametrize('value', [pytest.param('', id='empty'), pytest.param('bad\nlabel', id='control'),
    pytest.param('x' * 1025, id='over-budget'), pytest.param(True, id='non-text'),
    pytest.param('a\x85b', id='unicode-c1'), pytest.param('a\u2028b', id='unicode-line'),
    pytest.param('a\u2029b', id='unicode-paragraph'), pytest.param('a\u202eb', id='unicode-bidi'),
    pytest.param('a\ud800b', id='unicode-surrogate')])
def test_declaration_refuses_invalid_identity_fields(field, value):
    fields = {'name': 'cancel', 'method': 'tasks/cancel', 'schema_json': b'{}', 'handler': handler}
    fields[field] = value
    with pytest.raises(ValueError, match='bounded control-free text'):
        NativeAction(**fields)


@pytest.mark.parametrize('field', ['action', 'method', 'task_handle', 'owner', 'tool_name',
    'registration_id', 'attribution_kind', 'view_label', 'conversation_id'])
@pytest.mark.parametrize('value', [pytest.param('', id='empty'), pytest.param('bad\nlabel', id='control'),
    pytest.param('x' * 1025, id='over-budget'), pytest.param(True, id='non-text'),
    pytest.param('a\x85b', id='unicode-c1'), pytest.param('a\u2028b', id='unicode-line'),
    pytest.param('a\u2029b', id='unicode-paragraph'), pytest.param('a\u202eb', id='unicode-bidi'),
    pytest.param('a\ud800b', id='unicode-surrogate')])
def test_context_refuses_invalid_identity_fields(context, field, value):
    with pytest.raises(ValueError, match='bounded control-free text'):
        replace(context, **{field: value})


@pytest.mark.parametrize('field', ['remote_task_digest', 'schema_digest', 'attribution_digest'])
@pytest.mark.parametrize('value', [None, '0' * 63, 'A' * 64, True])
def test_context_requires_original_digest_shapes(context, field, value):
    with pytest.raises(ValueError, match='SHA-256 digest'):
        replace(context, **{field: value})


@pytest.mark.parametrize('revision', [-1, True, None, 1.0, '1'])
def test_context_refuses_non_integer_or_negative_revision(context, revision):
    with pytest.raises(ValueError, match='nonnegative integer'):
        replace(context, input_revision=revision)


@pytest.mark.parametrize('kind', ['HTTP request', '服务', '../signal', 'SIGNAL', 'signal/caller', '0caller'])
def test_attribution_source_kind_remains_a_token(context, kind):
    with pytest.raises(ValueError, match='bounded token'):
        replace(context, attribution_kind=kind)


def test_identity_serialization_has_no_native_params_or_remote_id(context):
    wire = context.to_dict()
    assert not {'params', 'arguments', 'inputResponses', 'taskId'} & wire.keys()
    assert 'private remote task ID' not in json.dumps(wire)
    wire['task_handle'] = 'replacement'
    assert context.task_handle == 'local-task'
    with pytest.raises(FrozenInstanceError):
        context.input_revision = 4
    assert not hasattr(context, '__dict__')


def test_hook_action_context_and_decidable_params_use_distinct_slots(context):
    inputs = {'inputResponses': {'original-request': {'action': 'decline'}}}
    value = HookInput('session', HookEvent.PRE_TOOL_USE.value, tool_name='original_tool',
        feature_name='MCPAgent', tool_input=inputs, native_action=context)
    wire = value.to_dict()
    assert 'native_action' in wire
    assert wire['native_action'] == context.to_dict()
    assert wire['tool_input'] == inputs
    assert HookOutput.modify({}).updated_input == {}
    wire['native_action']['owner'] = 'different-owner'
    assert value.native_action is context and context.owner == 'mcp:original'
    assert 'native_action' not in HookInput('session', HookEvent.PRE_TOOL_USE.value).to_dict()


def test_context_never_becomes_a_provider_tool_call_field(context):
    call = ToolCall('original-call', 'original_tool', {'query': 'original'})
    assert asdict(call) == {'id': 'original-call', 'name': 'original_tool', 'arguments': {'query': 'original'}}
    assert context.task_handle not in json.dumps(asdict(call))


def test_ordinary_tool_has_an_empty_read_only_action_declaration():
    class Ordinary(AgentTool):
        name = 'ordinary'
        schema = ToolSchema(name='ordinary', description='Ordinary tool', category=ToolCategory.UTILITY)

        async def execute(self, **kwargs):
            return kwargs

    tool = Ordinary()
    assert dict(tool.native_actions) == {}
    with pytest.raises(TypeError):
        tool.native_actions['cancel'] = NativeAction('cancel', 'tasks/cancel', b'{}', handler)


@pytest.mark.asyncio
async def test_declaration_retains_the_original_handler_and_context(context):
    declaration = NativeAction('cancel', 'tasks/cancel', b'{"type":"object"}', handler)
    assert await declaration.handler(context, {}) == ('local-task', {})


@pytest.mark.asyncio
async def test_async_callable_objects_and_partial_handlers_are_supported(context):
    for target in (handler, partial(handler), _AsyncHandler(), partial(_AsyncHandler())):
        declaration = NativeAction('cancel', 'tasks/cancel', b'{}', target)
        assert declaration.handler is target
        assert await declaration.handler(context, {}) == ('local-task', {})


def test_native_action_signature_requires_separate_context_and_operation_key(context):
    signature = inspect.signature(GovernedToolBinding.invoke_action)
    for name in ('native_action', 'session_id', 'operation_id'):
        parameter = signature.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
    signature.bind(object(), 'cancel', {}, native_action=context,
                   session_id='owned-session', operation_id='owned-intent')
    with pytest.raises(TypeError, match='native_action'):
        signature.bind(object(), 'cancel', {}, session_id='owned-session', operation_id='owned-intent')
