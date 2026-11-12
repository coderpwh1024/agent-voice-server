from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.schemas.location import DeviceLocation
from app.services.location import device_location, device_location_scope, needs_device_location


def test_location_intent_uses_device_only_for_current_place():
    assert needs_device_location("我现在在哪里？")
    assert needs_device_location("请问附近有什么餐馆？")
    assert needs_device_location("从我这里去天安门")
    assert needs_device_location("给我推荐附近的地铁站")
    assert needs_device_location("Where am I")
    assert needs_device_location("Find restaurants near me")
    assert needs_device_location("这里附近有什么地铁站")
    assert not needs_device_location("王府井附近有什么餐馆？")
    assert not needs_device_location("北京天气怎么样？")
    assert not needs_device_location("解释一下‘当前位置’是什么意思")
    assert not needs_device_location("请解释一下当前位置")


def test_device_location_is_fresh_and_scoped_to_one_run():
    location = DeviceLocation(
        latitude=39.9,
        longitude=116.4,
        accuracy_meters=15,
        captured_at=datetime.now(UTC),
    )
    with device_location_scope(location):
        assert device_location() is location
    assert device_location() is None
    assert not DeviceLocation(
        latitude=39.9,
        longitude=116.4,
        accuracy_meters=15,
        captured_at=datetime.now(UTC) - timedelta(minutes=6),
    ).is_fresh()
    with pytest.raises(ValidationError):
        DeviceLocation(
            latitude=91, longitude=116.4, accuracy_meters=15, captured_at=datetime.now(UTC)
        )
