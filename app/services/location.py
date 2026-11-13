"""Keep one-turn device location outside checkpoint state and run metadata."""

import math
import re
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.schemas.location import DeviceLocation

_current: ContextVar[DeviceLocation | None] = ContextVar("device_location", default=None)


def amap_coordinates(location: DeviceLocation) -> tuple[float, float]:
    """Convert native GPS coordinates to GCJ-02 before querying Amap."""
    longitude, latitude = location.longitude, location.latitude
    if location.coordinate_system == "gcj02" or not (
        72.004 <= longitude <= 137.8347 and 0.8293 <= latitude <= 55.8271
    ):
        return longitude, latitude
    offset_x, offset_y = longitude - 105, latitude - 35
    latitude_delta = (
        -100
        + 2 * offset_x
        + 3 * offset_y
        + 0.2 * offset_y**2
        + 0.1 * offset_x * offset_y
        + 0.2 * math.sqrt(abs(offset_x))
    )
    longitude_delta = (
        300
        + offset_x
        + 2 * offset_y
        + 0.1 * offset_x**2
        + 0.1 * offset_x * offset_y
        + 0.1 * math.sqrt(abs(offset_x))
    )
    shared = (20 * math.sin(6 * offset_x * math.pi) + 20 * math.sin(2 * offset_x * math.pi)) * 2 / 3
    latitude_delta += (
        shared
        + (
            20 * math.sin(offset_y * math.pi)
            + 40 * math.sin(offset_y * math.pi / 3)
            + 160 * math.sin(offset_y * math.pi / 12)
            + 320 * math.sin(offset_y * math.pi / 30)
        )
        * 2
        / 3
    )
    longitude_delta += (
        shared
        + (
            20 * math.sin(offset_x * math.pi)
            + 40 * math.sin(offset_x * math.pi / 3)
            + 150 * math.sin(offset_x * math.pi / 12)
            + 300 * math.sin(offset_x * math.pi / 30)
        )
        * 2
        / 3
    )
    radians = math.radians(latitude)
    eccentricity = 0.006693421622965943
    magic = 1 - eccentricity * math.sin(radians) ** 2
    root = math.sqrt(magic)
    latitude_delta = (
        latitude_delta * 180 / (6378245 * (1 - eccentricity) / (magic * root) * math.pi)
    )
    longitude_delta = longitude_delta * 180 / (6378245 / root * math.cos(radians) * math.pi)
    return round(longitude + longitude_delta, 6), round(latitude + latitude_delta, 6)


def needs_device_location(text: str) -> bool:
    normalized = "".join(text.lower().split())
    if any(phrase in normalized for phrase in ("什么意思", "什么含义", "怎么翻译", "解释一下")):
        return False
    if normalized.startswith(("什么是", "定义", "翻译", "举例")):
        return False
    if re.search(r"我现在位于|定位我", normalized):
        return True
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
    return bool(
        re.search(
            r"^(?:(?:请问|请|帮我|帮忙|查找|查一下|看看|找|搜|搜索|推荐|给我|现在|目前|在)[，,]*)*"
            r"(?:附近|周边|周围)",
            normalized,
        )
        or re.search(r"(?:推荐|搜索|找|看看|查找|查询|查一下)附近", normalized)
    )


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
