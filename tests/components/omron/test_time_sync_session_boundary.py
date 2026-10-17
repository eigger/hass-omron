"""CTS must not run while pairing retains the Omron memory session."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.omron.omron_ble import time_sync


@pytest.mark.asyncio
@pytest.mark.parametrize("pass_transport", [True, False])
async def test_retained_memory_session_skips_cts_after_eeprom_sync_fails(
    monkeypatch, pass_transport
):
    client = SimpleNamespace(is_connected=True)
    transport = SimpleNamespace(
        memory=SimpleNamespace(memory_session_active=False)
    )
    config = SimpleNamespace(supports_eeprom_time_sync=True)

    async def failed_eeprom_sync(*args, **kwargs):
        transport.memory.memory_session_active = True
        return False

    monkeypatch.setattr(time_sync, "_sync_eeprom_with_session", failed_eeprom_sync)
    cts = AsyncMock(return_value=True)
    monkeypatch.setattr(time_sync, "_sync_time_via_cts", cts)

    result = await time_sync.async_sync_device_time(
        client,
        "HEM-test",
        config,
        transport if pass_transport else None,
        leave_memory_session_open=True,
    )

    assert result is False
    cts.assert_not_awaited()
