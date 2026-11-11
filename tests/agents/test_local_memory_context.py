import asyncio
from collections.abc import AsyncGenerator

import pytest

from app.schemas.local_memory import LocalMemoryContext
from app.services.memory.local_context import DeviceMemory, device_memory, device_memory_events


@pytest.mark.asyncio
async def test_interleaved_streams_do_not_expose_another_users_memory():
    async def events(user: str) -> AsyncGenerator[str, None]:
        for _ in range(2):
            assert device_memory(user, "thread") is not None
            yield user

    streams = [
        device_memory_events(events(user), DeviceMemory(user, "thread", LocalMemoryContext()))
        for user in ["alice", "bob"]
    ]
    for _ in range(2):
        for stream, user in zip(streams, ["alice", "bob"], strict=True):
            assert await anext(stream) == user
            assert device_memory(user, "thread") is None
    for stream in streams:
        await stream.aclose()


@pytest.mark.asyncio
async def test_stream_can_be_closed_by_disconnect_cleanup_in_another_task():
    closed = []

    async def events() -> AsyncGenerator[str, None]:
        try:
            yield "event"
        finally:
            assert device_memory("alice", "thread") is not None
            closed.append(True)

    stream = device_memory_events(events(), DeviceMemory("alice", "thread", LocalMemoryContext()))
    assert await anext(stream) == "event"
    await asyncio.create_task(stream.aclose())
    assert closed == [True]
    assert device_memory("alice", "thread") is None
