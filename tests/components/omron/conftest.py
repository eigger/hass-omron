"""Omron integration domain fixtures."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN
from tests.components.omron.bt import service_info

# <!-- hass-standards:domain-fixtures:start -->
# Use pytest_homeassistant_custom_component.common.MockConfigEntry for
# mock_config_entry when the integration uses config entries.
# Define mock_client only when an external communication client is present.
# Mock external communication boundaries, never Home Assistant core modules.
# Keep integration-specific fixtures below this managed block.
# <!-- hass-standards:domain-fixtures:end -->

DEFAULT_ADDRESS = "AA:BB:CC:DD:EE:FF"
DEFAULT_MODEL = "HEM-7155T"


@pytest.fixture
def omron_service_info() -> BluetoothServiceInfoBleak:
    """Return a default Omron BluetoothServiceInfoBleak advertisement."""
    return service_info(address=DEFAULT_ADDRESS, name="BLESmart_0000011F000000000000")


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return an unadded MockConfigEntry for a default Omron cuff."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEFAULT_ADDRESS,
        title=f"{DEFAULT_MODEL} EEFF",
        data={CONF_DEVICE_MODEL: DEFAULT_MODEL},
    )


@pytest.fixture(autouse=True)
async def enable_event_loop_debug() -> None:
    """Enable event loop debug mode on the running loop.

    Overrides the plugin fixture of the same name. The pytest-homeassistant-
    custom-component release for the minimum Home Assistant (2025.12) calls
    ``asyncio.get_event_loop()`` from a synchronous fixture, which raises
    "There is no current event loop" for this suite's synchronous protocol
    tests. Newer plugin releases use the running loop.
    """
    asyncio.get_running_loop().set_debug(True)


@pytest.fixture
def omron_config_entry(mock_config_entry: MockConfigEntry) -> MockConfigEntry:
    """Keep the integration-specific entry fixture as a compatibility alias."""
    return mock_config_entry


class MockBleakClient:
    """External connected BLE link for reconnect and settle behavior tests."""

    def __init__(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False


@pytest.fixture
def mock_client() -> MockBleakClient:
    """Return an external GATT client without replacing Home Assistant internals."""
    return MockBleakClient()
