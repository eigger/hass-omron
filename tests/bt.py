"""Bluetooth advertisements for integration tests.

``pytest-homeassistant-custom-component`` does not ship Home Assistant's
bluetooth test helpers. ``service_info`` builds the object the config flow
and the advertisement callback consume.
"""

from __future__ import annotations

import time
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.components.bluetooth import (
    SOURCE_LOCAL,
    BluetoothServiceInfoBleak,
)

_ADVERTISEMENT_DEFAULTS: dict[str, Any] = {
    "local_name": "",
    "manufacturer_data": {},
    "service_data": {},
    "service_uuids": [],
    "rssi": -127,
    "platform_data": ((),),
    "tx_power": -127,
}


def service_info(
    address: str,
    *,
    name: str = "",
    manufacturer_data: dict[int, bytes] | None = None,
    service_uuids: list[str] | None = None,
    rssi: int = -60,
    connectable: bool = True,
) -> BluetoothServiceInfoBleak:
    """A ``BluetoothServiceInfoBleak`` as the bluetooth integration would deliver it."""
    advertisement = AdvertisementData(
        **{
            **_ADVERTISEMENT_DEFAULTS,
            "local_name": name or None,
            "manufacturer_data": manufacturer_data or {},
            "service_uuids": service_uuids or [],
            "rssi": rssi,
        }
    )
    return BluetoothServiceInfoBleak(
        name=name or address,
        address=address,
        rssi=rssi,
        manufacturer_data=advertisement.manufacturer_data,
        service_data=advertisement.service_data,
        service_uuids=advertisement.service_uuids,
        source=SOURCE_LOCAL,
        device=BLEDevice(address=address, name=name or None, details={}),
        advertisement=advertisement,
        connectable=connectable,
        time=time.monotonic(),
        tx_power=advertisement.tx_power,
    )
