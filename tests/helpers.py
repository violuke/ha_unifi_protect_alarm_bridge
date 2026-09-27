"""Shared test data. Every value is a placeholder, never real console data."""

from __future__ import annotations

from datetime import datetime
import json
import struct
from typing import Any
import zlib

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_protect_alarm_bridge.api import ArmProfile, ConsoleInfo
from custom_components.unifi_protect_alarm_bridge.const import CONF_PROFILE_AWAY, DOMAIN

HOST = "192.0.2.1"
USERNAME = "test-user"
PASSWORD = "test-password"
MAC = "AABBCCDDEEFF"
UNIQUE_ID = "aa:bb:cc:dd:ee:ff"
AWAY_ID = "00000000-0000-4000-8000-000000000001"
HOME_ID = "00000000-0000-4000-8000-000000000002"
OTHER_ID = "00000000-0000-4000-8000-000000000003"
TITLES = {AWAY_ID: "Away", HOME_ID: "Home", OTHER_ID: "Other"}
ENTITY_ID = "alarm_control_panel.test_console"

# Shape of GET /proxy/protect/api/nvr, trimmed to the fields we use.
CONSOLE_JSON: dict[str, Any] = {
    "id": "000000000000000000000001",
    "mac": MAC,
    "name": "Test Console",
    "type": "UDM-PRO",
    "version": "7.2.105",
    "firmwareVersion": "5.1.33",
    "featureFlags": {"useExternalAlarmManager": True},
}
CONSOLE = ConsoleInfo.from_api(CONSOLE_JSON)

ENTRY_DATA = {
    CONF_HOST: HOST,
    CONF_USERNAME: USERNAME,
    CONF_PASSWORD: PASSWORD,
    CONF_VERIFY_SSL: False,
}


def profile_json(
    profile_id: str = AWAY_ID,
    title: str | None = None,
    state: str = "disarmed",
    activation_delay: int | None = 60,
    promotion_due: datetime | None = None,
) -> dict[str, Any]:
    """Return a profile in the shape of GET /api/v2/alarms/profiles."""
    data: dict[str, Any] = {
        "id": profile_id,
        "title": title or TITLES.get(profile_id, "Profile"),
        "activation_delay": activation_delay,
        "alarm_ids": ["00000000-0000-4000-8000-0000000000a1"],
        "created_at": "2026-09-11T00:17:58Z",
        "updated_at": "2026-09-11T00:17:58Z",
        "state": state,
        "state_set_at": "2026-09-27T13:55:44Z",
    }
    if promotion_due is not None:
        data["state_promotion_due_at"] = promotion_due.isoformat().replace(
            "+00:00", "Z"
        )
    return data


def make_profile(**kwargs: Any) -> ArmProfile:
    """Return a parsed ArmProfile (same kwargs as profile_json)."""
    return ArmProfile.from_api(profile_json(**kwargs))


def titled_profile(profile_id: str, state: str) -> ArmProfile:
    """Return the profile the fake API would answer an action with."""
    return make_profile(profile_id=profile_id, state=state)


def encode_packet(action: Any, data: Any, *, deflate: bool = False) -> bytes:
    """Encode two JSON frames in Protect's binary websocket format."""
    packet = b""
    for packet_type, frame in ((1, action), (2, data)):
        payload = json.dumps(frame).encode()
        if deflate:
            payload = zlib.compress(payload)
        packet += struct.pack(">BBBBI", packet_type, 1, int(deflate), 0, len(payload))
        packet += payload
    return packet


def profile_packet(profile: dict[str, Any], *, deflate: bool = False) -> bytes:
    """Encode an externalArmProfile update as Protect sends it."""
    action = {
        "action": "update",
        "newUpdateId": "00000000-0000-4000-8000-0000000000f1",
        "modelKey": "externalArmProfile",
        "id": profile["id"],
    }
    return encode_packet(action, profile, deflate=deflate)


def mock_config_entry(options: dict[str, str] | None = None) -> MockConfigEntry:
    """Return a config entry for the placeholder console."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test Console",
        unique_id=UNIQUE_ID,
        data=dict(ENTRY_DATA),
        options=options if options is not None else {CONF_PROFILE_AWAY: AWAY_ID},
    )
