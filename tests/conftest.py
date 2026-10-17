"""Repository-wide Home Assistant test configuration."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Let Home Assistant load custom_components/omron when a test uses ``hass``."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")


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
