# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.
"""A logical refusal is data; only original-source EOF/join permits projection."""

from __future__ import annotations

import subprocess
import sys

from collections.abc import AsyncIterable
from pathlib import Path
from typing import cast

import anyio
import pytest

from kestrel_sdk.native_a2a.utils.errors import A2AError, UnsupportedOperationError
from kestrel_sdk.native_a2a.utils.source_lifetime import standalone_source_lifetime
from kestrel_sdk.native_a2a.utils.source_ownership import SourceAcquisition

from tests.native_a2a.source_witness import DescriptorSource


def test_actual_registered_host_control_is_not_projected_as_protocol_data() -> (
    None
):
    # A real process owns this registration; never replace the process-global
    # policy of other tests with a pretend classifier or leak our registration.
    child = subprocess.run(
        [
            sys.executable,
            '-I',
            '-B',
            '-c',
            """
import anyio
from kestrel_sdk.features.host_errors import register_control_error_predicate
from kestrel_sdk.native_a2a.utils.errors import UnsupportedOperationError
from kestrel_sdk.native_a2a.utils.source_lifetime import standalone_source_lifetime
from kestrel_sdk.native_a2a.utils.stream_refusal import StreamRefusal, drain_stream_refusal

original = UnsupportedOperationError("actual registered host control")
register_control_error_predicate(lambda error: error is original)

async def source():
    yield StreamRefusal(original)

async def main():
    async with standalone_source_lifetime(lambda resources: source()) as reader:
        refusal = await anext(reader)
        try:
            await drain_stream_refusal(reader, refusal)
        except UnsupportedOperationError as error:
            assert error is original and error.__cause__ is None
        else:
            raise AssertionError("Host control was projected as ordinary protocol data")

anyio.run(main)
""",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert child.returncode == 0, (child.stdout, child.stderr)
    assert (
        'ResourceWarning' not in child.stderr
        and 'Exception ignored' not in child.stderr
    )


def test_refusal_is_an_explicit_value_and_does_not_map_or_format_its_error() -> (
    None
):
    from kestrel_sdk.native_a2a.utils.stream_refusal import StreamRefusal, map_stream_item

    error = UnsupportedOperationError('expected logical refusal')
    refusal = StreamRefusal(error)
    mapped: list[str] = []

    def serialize(value: str) -> str:
        mapped.append(value)
        return value.upper()

    assert map_stream_item(refusal, serialize) is refusal
    assert mapped == []
    assert map_stream_item('event', serialize) == 'EVENT'
    assert mapped == ['event'] and refusal.error is error
    assert error.__cause__ is None
    with pytest.raises(TypeError):
        StreamRefusal(cast(A2AError, object()))


def test_http_refusal_retains_the_actual_status_and_challenge() -> None:
    from kestrel_sdk.native_a2a.utils.stream_refusal import StreamRefusal
    from starlette.exceptions import HTTPException

    error = HTTPException(
        401, detail='proof refused', headers={'WWW-Authenticate': 'original'}
    )
    refusal = StreamRefusal(error)
    assert refusal.error is error
    assert error.status_code == 401
    assert error.headers == {'WWW-Authenticate': 'original'}
    assert error.__cause__ is None
    with pytest.raises(TypeError):
        StreamRefusal(cast(A2AError, OSError('not a logical refusal')))


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
@pytest.mark.parametrize('fail_close', [False, True])
def test_refusal_projection_waits_for_original_retirement_and_preserves_late_failure(
    tmp_path: Path,
    backend: str,
    fail_close: bool,
) -> None:
    from kestrel_sdk.native_a2a.utils.stream_refusal import StreamRefusal, drain_stream_refusal

    async def run() -> None:
        error = UnsupportedOperationError('expected logical refusal')
        refusal = StreamRefusal(error)
        sources: list[DescriptorSource[object]] = []

        def acquire(resources: SourceAcquisition) -> AsyncIterable[object]:
            source = DescriptorSource(
                tmp_path / 'refusal', backend, (refusal,), fail_close=fail_close
            )
            sources.append(source)
            resources.retain(source, source.aclose)
            return source

        if fail_close:
            with pytest.raises(OSError) as root:
                async with standalone_source_lifetime(acquire) as reader:
                    assert await anext(reader) is refusal
                    with pytest.raises(OSError) as close:
                        await drain_stream_refusal(reader, refusal)
            assert root.value is close.value is sources[0].close_error
        else:
            async with standalone_source_lifetime(acquire) as reader:
                assert await anext(reader) is refusal
                assert await drain_stream_refusal(reader, refusal) is error
                sources[0].assert_original_retired()
        sources[0].assert_original_retired()
        assert error.__cause__ is None

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_refusal_cannot_acknowledge_a_blocked_original_close(
    tmp_path: Path, backend: str
) -> None:
    from kestrel_sdk.native_a2a.utils.stream_refusal import StreamRefusal, drain_stream_refusal

    async def run() -> None:
        closing, release, projected = (
            anyio.Event(),
            anyio.Event(),
            anyio.Event(),
        )
        error = UnsupportedOperationError('expected logical refusal')
        refusal = StreamRefusal(error)
        sources: list[DescriptorSource[object]] = []

        def acquire(resources: SourceAcquisition) -> AsyncIterable[object]:
            source = DescriptorSource(
                tmp_path / 'blocked',
                backend,
                (refusal,),
                closing=closing,
                release=release,
            )
            sources.append(source)
            resources.retain(source, source.aclose)
            return source

        with anyio.fail_after(5):
            async with standalone_source_lifetime(acquire) as reader:
                assert await anext(reader) is refusal

                async def project() -> None:
                    assert await drain_stream_refusal(reader, refusal) is error
                    projected.set()

                async with anyio.create_task_group() as group:
                    group.start_soon(project)
                    await closing.wait()
                    assert not projected.is_set()
                    release.set()
            assert projected.is_set()
            sources[0].assert_original_retired()

    anyio.run(run, backend=backend)


@pytest.mark.parametrize('backend', ['asyncio', 'trio'])
def test_refusal_followed_by_another_item_is_not_a_terminal_outcome(
    tmp_path: Path, backend: str
) -> None:
    from kestrel_sdk.native_a2a.utils.stream_refusal import StreamRefusal, drain_stream_refusal

    async def run() -> None:
        refusal = StreamRefusal(
            UnsupportedOperationError('expected logical refusal')
        )
        sources: list[DescriptorSource[object]] = []

        def acquire(resources: SourceAcquisition) -> AsyncIterable[object]:
            source = DescriptorSource(
                tmp_path / 'extra-item', backend, (refusal, object())
            )
            sources.append(source)
            resources.retain(source, source.aclose)
            return source

        with pytest.raises(ValueError, match='terminal stream refusal'):
            async with standalone_source_lifetime(acquire) as reader:
                assert await anext(reader) is refusal
                await drain_stream_refusal(reader, refusal)
        sources[0].assert_original_retired()

    anyio.run(run, backend=backend)
