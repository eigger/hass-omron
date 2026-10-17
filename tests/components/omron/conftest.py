"""Omron integration domain fixtures."""

from __future__ import annotations

import pytest
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN
from tests.components.omron.bt import service_info

DEFAULT_ADDRESS = "AA:BB:CC:DD:EE:FF"
DEFAULT_MODEL = "HEM-7155T"


@pytest.fixture
def omron_service_info() -> BluetoothServiceInfoBleak:
    """Return a default Omron BluetoothServiceInfoBleak advertisement."""
    return service_info(address=DEFAULT_ADDRESS, name="BLESmart_0000011F000000000000")


@pytest.fixture
def omron_config_entry() -> MockConfigEntry:
    """Return an unadded MockConfigEntry for a default Omron cuff."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEFAULT_ADDRESS,
        title=f"{DEFAULT_MODEL} EEFF",
        data={CONF_DEVICE_MODEL: DEFAULT_MODEL},
    )
