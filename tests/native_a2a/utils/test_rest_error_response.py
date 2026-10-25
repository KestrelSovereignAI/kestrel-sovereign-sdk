# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.
"""Actual native REST error responses preserve their media, status and body."""

import json

import pytest
from starlette.responses import JSONResponse

from kestrel_sdk.native_a2a.utils.error_handlers import rest_error_handler
from kestrel_sdk.native_a2a.utils.errors import InvalidRequestError


@pytest.mark.asyncio
@pytest.mark.parametrize('error,code,status,reason', [
    (InvalidRequestError(message='Bad request'), 400, 'INVALID_ARGUMENT', 'INVALID_REQUEST'),
    (ValueError('Unexpected error'), 500, 'INTERNAL', None),
])
async def test_real_rest_error_response(error, code, status, reason):
    @rest_error_handler
    async def reject():
        raise error

    response = await reject()
    assert isinstance(response, JSONResponse)
    assert response.status_code == code
    assert response.headers['content-type'] == 'application/a2a+json'
    payload = json.loads(response.body)['error']
    assert payload['code'] == code and payload['status'] == status
    if reason is not None:
        assert payload['message'] == 'Bad request'
        assert payload['details'][0] == {
            '@type': 'type.googleapis.com/google.rpc.ErrorInfo',
            'reason': reason, 'domain': 'a2a-protocol.org', 'metadata': {},
        }
    else:
        assert payload == {'code': 500, 'status': 'INTERNAL', 'message': 'unknown exception'}
