# Modified by Kestrel: isolated SDK namespace; see VENDORED.json.
"""Canonical version parsing shared by client selection and server handlers."""

import pytest

from kestrel_sdk.native_a2a.utils.version_validator import validate_version
from kestrel_sdk.native_a2a.utils.versioning import major_minor
from packaging.version import InvalidVersion


@pytest.mark.parametrize(
    ('version', 'release'),
    [
        ('1.0', (1, 0)),
        ('1.0.1', (1, 0)),
        ('1.0.999', (1, 0)),
        ('1.1', (1, 1)),
        ('2.0', (2, 0)),
    ],
)
def test_protocol_release(version: str, release: tuple[int, int]) -> None:
    assert major_minor(version) == release


def test_invalid_protocol_version() -> None:
    with pytest.raises(InvalidVersion):
        major_minor('invalid')


def test_invalid_handler_configuration_fails_at_decoration() -> None:
    with pytest.raises(InvalidVersion):
        validate_version('invalid')
