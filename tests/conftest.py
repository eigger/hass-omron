"""Test configuration.

Protocol tests import ``omron_ble`` and do not start Home Assistant.
Integration tests request the ``hass`` fixture; only those enable this
custom component.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Let Home Assistant load custom_components/omron when a test uses ``hass``."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")
