"""Tests for diagnostics redaction."""

import json

from custom_components.unifi_protect_alarm_bridge.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import setup_entry
from .helpers import AWAY_ID, HOST, PASSWORD, USERNAME, mock_config_entry


async def test_diagnostics_are_redacted(hass, mock_client) -> None:
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    entry.runtime_data.last_unexpected_payload = [
        {"id": AWAY_ID, "title": "Jane's house", "state": "odd"}
    ]

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    dumped = json.dumps(diagnostics)
    for secret in (HOST, USERNAME, PASSWORD, "Test Console", "Jane's house", '"Away"'):
        assert secret not in dumped
    assert diagnostics["profiles"][AWAY_ID]["state"] == "disarmed"
    assert diagnostics["console"]["protect_version"] == "7.2.105"
    assert diagnostics["console"]["unifi_os_version"] == "5.1.33"
    assert diagnostics["push"] == {
        "connected": False,
        "last_message_at": None,
        "reconnect_count": 0,
    }
    assert diagnostics["polling"]["last_update_success"] is True
    assert diagnostics["last_unexpected_payload"][0]["state"] == "odd"
