"""Keep device memory outside LangGraph checkpoint state and run configuration."""

from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from app.schemas.local_memory import LocalMemoryContext


@dataclass(frozen=True)
class DeviceMemory:
    user_id: str
    thread_id: str
    context: LocalMemoryContext


_current: ContextVar[DeviceMemory | None] = ContextVar("device_memory", default=None)


@contextmanager
def device_memory_scope(value: DeviceMemory | None) -> Iterator[None]:
    token = _current.set(value)
    try:
        yield
    finally:
        _current.reset(token)


def device_memory(user_id: str, thread_id: str) -> DeviceMemory | None:
    value = _current.get()
    if value and (value.user_id != user_id or value.thread_id != thread_id):
        raise ValueError("Device memory scope does not match the run")
    return value


async def device_memory_events[T](
    events: AsyncGenerator[T, None], value: DeviceMemory | None
) -> AsyncGenerator[T, None]:
    try:
        while True:
            with device_memory_scope(value):
                try:
                    event = await anext(events)
                except StopAsyncIteration:
                    return
            yield event
    finally:
        with device_memory_scope(value):
            await events.aclose()
