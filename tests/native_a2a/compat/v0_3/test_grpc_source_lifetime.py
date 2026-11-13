# Modified by Kestrel: isolated SDK namespace; see kestrel_sdk/native_a2a/VENDORED.json.
"""Actual grpc.aio coroutine writers, original Tasks and file descriptors."""

from __future__ import annotations

import asyncio
import os

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import anyio
import grpc
import pytest

from kestrel_sdk.native_a2a.compat.v0_3 import a2a_v0_3_pb2, a2a_v0_3_pb2_grpc
from kestrel_sdk.native_a2a.compat.v0_3.grpc_handler import CompatGrpcHandler
from kestrel_sdk.native_a2a.server.events import Event
from kestrel_sdk.native_a2a.types.a2a_pb2 import Message, Part, Role
from kestrel_sdk.native_a2a.utils.source_lifetime import (
    SourceDemand,
    SourceFactory,
    standalone_source_lifetime,
)

from tests.native_a2a.source_witness import (
    DescriptorSource,
    StreamRequestHandler,
    original_owner,
)


@pytest.mark.asyncio
@pytest.mark.parametrize('subscribe', [False, True])
async def test_real_grpc_writer_joins_original_before_final_status(
    tmp_path: Path, subscribe: bool
) -> None:
    sources: list[DescriptorSource[Event]] = []

    def acquire() -> DescriptorSource[Event]:
        source = DescriptorSource(
            tmp_path / 'source',
            'asyncio',
            (
                Message(
                    message_id='output',
                    role=Role.ROLE_AGENT,
                    parts=[Part(text='real output')],
                ),
            ),
        )
        sources.append(source)
        return source

    server = grpc.aio.server()
    a2a_v0_3_pb2_grpc.add_A2AServiceServicer_to_server(
        CompatGrpcHandler(StreamRequestHandler(acquire)), server
    )
    port = server.add_insecure_port('127.0.0.1:0')
    assert port > 0
    await server.start()
    try:
        with anyio.fail_after(5):
            async with grpc.aio.insecure_channel(
                f'127.0.0.1:{port}'
            ) as channel:
                stub = a2a_v0_3_pb2_grpc.A2AServiceStub(channel)
                if subscribe:
                    call = stub.TaskSubscription(
                        a2a_v0_3_pb2.TaskSubscriptionRequest(name='tasks/one')
                    )
                else:
                    call = stub.SendStreamingMessage(
                        a2a_v0_3_pb2.SendMessageRequest(
                            request=a2a_v0_3_pb2.Message(
                                message_id='input',
                                role=a2a_v0_3_pb2.Role.ROLE_USER,
                            )
                        )
                    )
                responses = [item async for item in call]
                assert await call.code() == grpc.StatusCode.OK
        assert len(responses) == 1
        assert responses[0].msg.message_id == 'output'
        assert len(sources) == 1
        sources[0].assert_original_retired()
    finally:
        await server.stop(0)


@pytest.mark.asyncio
async def test_real_grpc_client_withdrawal_waits_for_blocked_original_close(
    tmp_path: Path,
) -> None:
    closing, release = anyio.Event(), anyio.Event()
    sources: list[DescriptorSource[Event]] = []
    roots: list[tuple[object, object]] = []

    def acquire() -> DescriptorSource[Event]:
        source = DescriptorSource(
            tmp_path / 'source',
            'asyncio',
            (Message(message_id='output', role=Role.ROLE_AGENT),),
            closing=closing,
            release=release,
        )
        sources.append(source)
        return source

    @asynccontextmanager
    async def observed_policy(
        acquire_source: SourceFactory[Event],
    ) -> AsyncIterator[SourceDemand[Event]]:
        roots.append(original_owner('asyncio'))
        async with standalone_source_lifetime(acquire_source) as reader:
            yield reader

    server = grpc.aio.server()
    a2a_v0_3_pb2_grpc.add_A2AServiceServicer_to_server(
        CompatGrpcHandler(
            StreamRequestHandler(acquire),
            source_lifetime_factory=observed_policy,
        ),
        server,
    )
    port = server.add_insecure_port('127.0.0.1:0')
    assert port > 0
    await server.start()
    try:
        with anyio.fail_after(5):
            async with grpc.aio.insecure_channel(
                f'127.0.0.1:{port}'
            ) as channel:
                call = a2a_v0_3_pb2_grpc.A2AServiceStub(
                    channel
                ).SendStreamingMessage(
                    a2a_v0_3_pb2.SendMessageRequest(
                        request=a2a_v0_3_pb2.Message(
                            message_id='input', role=a2a_v0_3_pb2.Role.ROLE_USER
                        )
                    )
                )
                assert (await call.read()).msg.message_id == 'output'
                call.cancel()
                try:
                    await closing.wait()
                    root_task = cast('asyncio.Task[None]', roots[0][0])
                    assert root_task.get_coro() is roots[0][1]
                    assert not root_task.done()
                    os.fstat(sources[0].fd)
                finally:
                    release.set()
                while not root_task.done():
                    await anyio.lowlevel.checkpoint()
                assert root_task.get_coro() is roots[0][1]
        sources[0].assert_original_retired()
    finally:
        release.set()
        await server.stop(0)
