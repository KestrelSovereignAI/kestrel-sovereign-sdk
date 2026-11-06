"""The public binding ABI requires an explicit intent key across transports."""

import inspect
import ast
from pathlib import Path
import re

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
    with pytest.raises(TypeError, match='operation_id'):
        signature.bind(object(), {}, session_id='owned-view', **unrelated_id)


def test_published_readme_invoke_examples_bind_the_current_public_contract():
    readme = (Path(__file__).resolve().parents[1] / 'README.md').read_text()
    examples = re.findall(r'\binvoke\(arguments,[^)]*\)', readme)
    assert examples, 'The public binding contract needs a documented call'
    for example in examples:
        call = ast.parse(example, mode='eval').body
        keywords = {keyword.arg: 'owned-value' for keyword in call.keywords}
        inspect.signature(GovernedToolBinding.invoke).bind(object(), {}, **keywords)
