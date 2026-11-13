# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.
"""Refusal demand views do not manufacture completion or empty protobufs."""

from __future__ import annotations

import os

from collections.abc import AsyncIterable
from pathlib import Path
from typing import cast

import anyio
import pytest

from kestrel_sdk.native_a2a.utils.errors import UnsupportedOperationError
from kestrel_sdk.native_a2a.utils.proto_utils import Event, to_stream_response
from kestrel_sdk.native_a2a.utils.source_lifetime import standalone_source_lifetime
from kestrel_sdk.native_a2a.utils.source_ownership import SourceAcquisition
from kestrel_sdk.native_a2a.utils.stream_refusal import ProtocolStreamDemand, StreamRefusal

from tests.native_a2a.source_witness import DescriptorSource


def test_profiles_without_refusal_projection_reject_it_instead_of_empty_protobuf() -> (
    None
):
    error = UnsupportedOperationError('expected logical refusal')
    with pytest.raises(
        UnsupportedOperationError, match='source-lifetime-aware HTTP dispatcher'
    ):
        to_stream_response(StreamRefusal(error))
    with pytest.raises(TypeError, match='Unsupported stream event type'):
        to_stream_response(cast(Event, object()))
    assert error.__cause__ is None


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_protocol_view_inspection_does_not_certify_pending_original_close(
    tmp_path: Path,
    backend: str,
) -> None:
    async def run() -> None:
        closing, release, projected = (
            anyio.Event(),
            anyio.Event(),
            anyio.Event(),
        )
        error = UnsupportedOperationError('expected logical refusal')
        sources: list[DescriptorSource[object]] = []

        def acquire(resources: SourceAcquisition) -> AsyncIterable[object]:
            source = DescriptorSource(
                tmp_path / 'pending-view',
                backend,
                (StreamRefusal(error),),
                closing=closing,
                release=release,
            )
            sources.append(source)
            resources.retain(source, source.aclose)
            return source

        with anyio.fail_after(5):
            async with standalone_source_lifetime(acquire) as reader:
                protocol = ProtocolStreamDemand(reader)
                assert not hasattr(protocol, 'aclose')

                async def project() -> None:
                    with pytest.raises(UnsupportedOperationError) as refused:
                        await anext(protocol)
                    assert refused.value is error
                    projected.set()

                async with anyio.create_task_group() as group:
                    group.start_soon(project)
                    await closing.wait()
                    protocol.raise_if_source_failed()
                    protocol.raise_if_retirement_failed()
                    os.fstat(sources[0].fd)
                    assert not projected.is_set()
                    release.set()
            assert projected.is_set()
            sources[0].assert_original_retired()

    anyio.run(run, backend=backend)
