"""The public binding ABI requires an explicit intent key across transports."""

import inspect
import pytest

from kestrel_sdk.tools import GovernedToolBinding


def test_operation_id_is_a_required_keyword_in_the_public_binding_contract():
    signature = inspect.signature(GovernedToolBinding.invoke)
    operation = signature.parameters['operation_id']
    assert operation.kind is inspect.Parameter.KEYWORD_ONLY
    assert operation.default is inspect.Parameter.empty
    signature.bind(object(), {}, session_id='owned-view', operation_id='owned-intent')


@pytest.mark.parametrize('unrelated_id', [{}, {'id': 'json-rpc-id'}, {'request_id': 'transport-id'}])
def test_transport_identity_cannot_substitute_for_an_operation_id(unrelated_id):
    signature = inspect.signature(GovernedToolBinding.invoke)
    with pytest.raises(TypeError):
        signature.bind(object(), {}, session_id='owned-view', **unrelated_id)
