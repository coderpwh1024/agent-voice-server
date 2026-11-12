"""Keep one-turn device location outside checkpoint state and run metadata."""

from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.schemas.location import DeviceLocation

_current: ContextVar[DeviceLocation | None] = ContextVar("device_location", default=None)


def needs_device_location(text: str) -> bool:
    normalized = "".join(text.lower().split())
    if any(phrase in normalized for phrase in ("什么意思", "什么含义", "怎么翻译", "解释一下")):
        return False
    if normalized.startswith(("什么是", "定义", "翻译", "举例")):
        return False
    if "whereami" in normalized or "nearme" in normalized:
        return True
    references = (
        "当前位置",
        "我的位置",
        "我现在的位置",
        "我现在在哪",
        "我在哪里",
        "我在哪",
        "我在什么地方",
        "我所处的位置",
        "我所在的位置",
        "我附近",
        "我周边",
        "我身边",
        "这里附近",
        "这里周边",
        "咱们附近",
        "我们附近",
        "离我最近",
        "距我",
        "距离我",
        "从我这里",
        "从这里出发",
    )
    if any(phrase in normalized for phrase in references):
        return True
    prefixes = ("", "请问", "帮我查", "帮我找", "查一下", "找一下", "给我推荐", "有没有")
    nearby = ("附近有什么", "附近的", "附近哪里", "附近有没有", "周边有什么")
    return any(normalized.startswith(prefix + phrase) for prefix in prefixes for phrase in nearby)


@contextmanager
def device_location_scope(value: DeviceLocation | None) -> Iterator[None]:
    token = _current.set(value)
    try:
        yield
    finally:
        _current.reset(token)


def device_location() -> DeviceLocation | None:
    return _current.get()


async def device_location_events[T](
    events: AsyncGenerator[T, None], value: DeviceLocation | None
) -> AsyncGenerator[T, None]:
    try:
        while True:
            with device_location_scope(value):
                try:
                    event = await anext(events)
                except StopAsyncIteration:
                    return
            yield event
    finally:
        with device_location_scope(value):
            await events.aclose()
