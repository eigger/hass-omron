"""The cuff clock follows the configured zone, not the process zone."""

import datetime as dt

import pytest

from custom_components.omron.omron_ble import clock
from custom_components.omron.omron_ble.driver import OmronDeviceDriver
from custom_components.omron.omron_ble.devices import get_device_config

SEOUL = dt.timezone(dt.timedelta(hours=9))
NEW_YORK = dt.timezone(dt.timedelta(hours=-5))


@pytest.fixture(autouse=True)
def _reset_provider():
    clock.set_time_zone_provider(None)
    yield
    clock.set_time_zone_provider(None)


def test_without_a_provider_the_process_zone_is_used() -> None:
    assert (
        clock.local_tz().utcoffset(None) == dt.datetime.now().astimezone().utcoffset()
    )


def test_the_provider_decides_the_zone() -> None:
    clock.set_time_zone_provider(lambda: SEOUL)
    assert clock.local_tz() is SEOUL
    assert clock.local_now().utcoffset() == dt.timedelta(hours=9)


def test_a_provider_returning_none_falls_back() -> None:
    clock.set_time_zone_provider(lambda: None)
    assert clock.local_tz() is not None


def test_wall_clock_is_the_configured_zones_and_naive() -> None:
    clock.set_time_zone_provider(lambda: SEOUL)
    wall = clock.local_wall_now()
    expected = dt.datetime.now(SEOUL).replace(tzinfo=None)
    assert wall.tzinfo is None
    assert abs(wall - expected) < dt.timedelta(seconds=5)


def test_two_zones_give_wall_clocks_that_differ_by_their_offset() -> None:
    clock.set_time_zone_provider(lambda: SEOUL)
    seoul = clock.local_wall_now()
    clock.set_time_zone_provider(lambda: NEW_YORK)
    new_york = clock.local_wall_now()
    delta = seoul - new_york
    assert abs(delta - dt.timedelta(hours=14)) < dt.timedelta(seconds=5)


def test_the_driver_stamps_the_configured_zone_on_the_device_time() -> None:
    clock.set_time_zone_provider(lambda: SEOUL)
    driver = OmronDeviceDriver(get_device_config("HEM-7142T2"))
    now = driver._now_func()
    assert now.tzinfo is None
    assert abs(now - dt.datetime.now(SEOUL).replace(tzinfo=None)) < dt.timedelta(
        seconds=5
    )


async def test_integration_setup_points_the_clock_at_the_ha_zone(hass) -> None:
    from homeassistant.setup import async_setup_component
    from homeassistant.util import dt as dt_util

    from custom_components.omron.const import DOMAIN

    await hass.config.async_set_time_zone("Asia/Seoul")
    assert await async_setup_component(hass, DOMAIN, {})
    assert clock.local_tz() is dt_util.DEFAULT_TIME_ZONE
    assert clock.local_now().utcoffset() == dt.timedelta(hours=9)
