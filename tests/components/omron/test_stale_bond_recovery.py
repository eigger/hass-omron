"""Issue #275 recovery through the HA flow, real BlueZ helpers and time sync.

The radio/D-Bus and secure memory transport are simulated. This checks setup
recovery and the EEPROM clock write, not the secure handshake's wire vectors
or BlueZ's real rediscovery after RemoveDevice.
"""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from dbus_fast.aio import message_bus
from dbus_fast.constants import MessageType
from homeassistant.config_entries import SOURCE_BLUETOOTH
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest

import custom_components.omron.config_flow as config_flow
from custom_components.omron.const import (
    CONF_DEVICE_MODEL,
    CONF_TRANSPORT_CREDENTIAL,
    DOMAIN,
)
from custom_components.omron.omron_ble import connection, session as session_module
from custom_components.omron.omron_ble.devices import get_device_config
from custom_components.omron.omron_ble.memory_protocol import MemoryProtocol
from custom_components.omron.session_handoff import discard_handoff_session
from tests.components.omron.bt import service_info

ADDRESS = "AA:BB:CC:DD:EE:75"
PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_75"
MODEL = "HEM-7188T1-LEO"
CREDENTIAL = bytes(range(16))


@pytest.mark.parametrize("remove_succeeds", [True, False], ids=["removed", "refused"])
async def test_stale_bond_recovery_reaches_initial_sync_without_restarting_ha(
    hass: HomeAssistant,
    enable_bluetooth: None,
    monkeypatch: pytest.MonkeyPatch,
    remove_succeeds: bool,
) -> None:
    """Only removing the stale host key lets the reset cuff serve its clock."""
    stale_bond = True
    events = []
    clock_writes = []
    config = get_device_config(MODEL)
    device = BLEDevice(ADDRESS, MODEL, {"path": PATH})

    class Bus:
        async def connect(self):
            return self

        def export(self, *args):
            pass

        def disconnect(self):
            pass

        async def call(self, message):
            nonlocal stale_bond
            body = []
            message_type = MessageType.METHOD_RETURN
            if message.member == "GetManagedObjects":
                body = [{PATH: {"org.bluez.Device1": {}}}]
            elif message.member == "Get":
                body = [SimpleNamespace(value=stale_bond)]
            elif message.member == "RemoveDevice":
                assert message.path == "/org/bluez/hci0"
                assert message.body == [PATH]
                events.append("remove")
                if remove_succeeds:
                    stale_bond = False
                else:
                    message_type = MessageType.ERROR
            else:
                assert message.member in (
                    "RegisterAgent",
                    "RequestDefaultAgent",
                    "UnregisterAgent",
                )
            return SimpleNamespace(message_type=message_type, body=body)

    class Link:
        address = ADDRESS
        is_connected = True
        services = [SimpleNamespace(uuid=config.parent_service_uuid)]

        async def start_notify(self, *args):
            pass

        async def stop_notify(self, *args):
            pass

        async def disconnect(self):
            self.is_connected = False

    link = Link()

    async def connect(*args, **kwargs):
        events.append("connect")
        if stale_bond:
            raise BleakError("failed to discover services, device disconnected")
        return link

    async def secure_unlock(session):
        assert session.client.is_connected and not stale_bond
        events.append("unlock")
        session._unlocked = True
        session._new_credential = CREDENTIAL

    async def open_memory(memory):
        assert memory._host.is_connected and memory._host._unlocked
        memory._memory_session_active = True

    async def read_memory(memory, address, size, block_size):
        assert memory.memory_session_active
        assert address == config.settings_read_address + 0x2C
        assert size == 16
        events.append("read_clock")
        # An invalid clock forces a real EEPROM time-sync write.
        return bytearray(16)

    async def write_memory(memory, address, data, *, block_size):
        assert memory.memory_session_active
        assert address == config.settings_write_address + 0x2C
        clock_writes.append(bytes(data))
        events.append("write_clock")

    monkeypatch.setattr(message_bus, "MessageBus", lambda **kwargs: Bus())
    monkeypatch.setattr(
        config_flow, "async_ble_device_from_address", lambda *args: device
    )
    monkeypatch.setattr(connection, "establish_connection", connect)
    monkeypatch.setattr(connection, "_POST_CONNECT_BOND_SETTLE_SEC", 0)
    monkeypatch.setattr(session_module, "_secure_unlock", secure_unlock)
    monkeypatch.setattr(MemoryProtocol, "open_memory_session", open_memory)
    monkeypatch.setattr(MemoryProtocol, "read_memory_range", read_memory)
    monkeypatch.setattr(MemoryProtocol, "write_memory_range", write_memory)

    # Establish the fault independently: the reset cuff refuses the old key.
    with pytest.raises(BleakError, match="failed to discover services"):
        await connect()
    events.clear()

    with ExitStack() as stack:
        stack.enter_context(
            patch("custom_components.omron.async_setup_entry", return_value=True)
        )
        stack.enter_context(
            patch("custom_components.omron.async_unload_entry", return_value=True)
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_BLUETOOTH},
            data=service_info(ADDRESS, name=MODEL),
        )
        if result["step_id"] == "bluetooth_confirm":
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={}
            )
        assert result["step_id"] == "select_model"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_DEVICE_MODEL: MODEL}
        )
        assert result["step_id"] == "pairing"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )

        if not remove_succeeds:
            assert result["type"] is FlowResultType.FORM
            assert result["errors"] == {"base": "pairing_failed"}
            assert not clock_writes
            assert not hass.config_entries.async_entries(DOMAIN)
            assert not hass.data.get(DOMAIN, {}).get("_setup_sessions")
            return

        assert result["type"] is FlowResultType.CREATE_ENTRY, result
        assert result["data"][CONF_TRANSPORT_CREDENTIAL] == CREDENTIAL.hex()
        assert events == ["remove", "connect", "unlock", "read_clock", "write_clock"]
        [clock] = clock_writes
        assert len(clock) == 16
        assert clock[14] == sum(clock[:14]) & 0xFF
        handoff = hass.data[DOMAIN]["_setup_sessions"][ADDRESS]
        assert handoff.client is link and handoff.is_connected
        assert handoff.memory.memory_session_active
        await hass.async_block_till_done()
        # Clean up the simulated peripheral without waiting for a real radio.
        handoff.memory._memory_session_active = False
        link.is_connected = False
        await discard_handoff_session(hass, ADDRESS)
        await hass.config_entries.async_remove(result["result"].entry_id)
