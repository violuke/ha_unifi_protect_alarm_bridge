"""Mode changes exercise the actual HA service, with synthetic console state."""

import asyncio
from dataclasses import replace
from itertools import permutations

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.unifi_protect_alarm_bridge.api import CannotConnect
from custom_components.unifi_protect_alarm_bridge.const import (
    CONF_PROFILE_AWAY,
    CONF_PROFILE_HOME,
    CONF_PROFILE_NIGHT,
)

from .conftest import setup_entry
from .helpers import (
    AWAY_ID,
    ENTITY_ID,
    HOME_ID,
    OTHER_ID,
    make_profile,
    mock_config_entry,
)

MODES = {"away": AWAY_ID, "home": HOME_ID, "night": OTHER_ID}
MAPPING = {
    CONF_PROFILE_AWAY: AWAY_ID,
    CONF_PROFILE_HOME: HOME_ID,
    CONF_PROFILE_NIGHT: OTHER_ID,
}


async def call(hass, mode):
    await hass.services.async_call(
        "alarm_control_panel",
        f"alarm_arm_{mode}",
        {"entity_id": ENTITY_ID},
        blocking=True,
    )


def console(mock_client, active="home", state="armed"):
    profiles = {
        pid: make_profile(profile_id=pid, state=state if mode == active else "disarmed")
        for mode, pid in MODES.items()
    }
    writes = []
    mock_client.async_get_profiles.side_effect = lambda: list(profiles.values())

    async def disarm(pid):
        writes.append(("disarm", pid))
        profiles[pid] = replace(profiles[pid], state="disarmed")
        return profiles[pid]

    async def arm(pid):
        writes.append(("arm", pid))
        profiles[pid] = replace(profiles[pid], state="armed")
        return profiles[pid]

    mock_client.async_disarm.side_effect = disarm
    mock_client.async_arm.side_effect = arm
    return profiles, writes


@pytest.mark.parametrize(("old", "new"), list(permutations(MODES, 2)))
async def test_switch_between_all_mapped_modes(hass, mock_client, old, new):
    _, writes = console(mock_client, old)
    await setup_entry(hass, mock_config_entry(MAPPING))
    await call(hass, new)
    assert writes == [("disarm", MODES[old]), ("arm", MODES[new])]
    assert hass.states.get(ENTITY_ID).state == f"armed_{new}"


async def test_switch_while_old_mode_is_arming(hass, mock_client):
    _, writes = console(mock_client, state="arming")
    await setup_entry(hass, mock_config_entry(MAPPING))
    await call(hass, "night")
    assert writes == [("disarm", HOME_ID), ("arm", OTHER_ID)]
    assert hass.states.get(ENTITY_ID).state == "armed_night"


async def test_failed_disarm_stops_before_new_arm(hass, mock_client):
    console(mock_client)
    mock_client.async_disarm.side_effect = CannotConnect("timeout")
    await setup_entry(hass, mock_config_entry(MAPPING))
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "away")
    assert err.value.translation_key == "mode_switch_failed"
    mock_client.async_disarm.assert_awaited_once_with(HOME_ID)
    mock_client.async_arm.assert_not_awaited()


@pytest.mark.parametrize(
    "after", ["active", "missing", "unknown", "breached", "offline"]
)
async def test_changed_or_unconfirmed_state_stops_switch(hass, mock_client, after):
    profiles, _ = console(mock_client)
    await setup_entry(hass, mock_config_entry(MAPPING))

    async def disarm(pid):
        profiles[pid] = replace(profiles[pid], state="disarmed")
        if after == "missing":
            del profiles[AWAY_ID]
        elif after == "offline":
            mock_client.async_get_profiles.side_effect = CannotConnect("offline")
        else:
            profiles[pid] = replace(
                profiles[pid],
                state={"active": "armed", "unknown": "new", "breached": "breached"}[
                    after
                ],
            )
        return make_profile(profile_id=pid)

    mock_client.async_disarm.side_effect = disarm
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "away")
    assert err.value.translation_key == "mode_switch_failed"
    mock_client.async_arm.assert_not_awaited()
    mock_client.async_disarm.assert_awaited_once_with(HOME_ID)


async def test_triggered_other_profile_requires_explicit_disarm(hass, mock_client):
    _, writes = console(mock_client, state="breached")
    await setup_entry(hass, mock_config_entry(MAPPING))
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "away")
    assert err.value.translation_key == "disarm_first"
    assert writes == []


async def test_multiple_active_profiles_are_disarmed_before_new_arm(hass, mock_client):
    profiles, writes = console(mock_client)
    profiles[OTHER_ID] = replace(profiles[OTHER_ID], state="armed")
    await setup_entry(hass, mock_config_entry(MAPPING))
    await call(hass, "away")
    assert writes == [("disarm", HOME_ID), ("disarm", OTHER_ID), ("arm", AWAY_ID)]
    assert hass.states.get(ENTITY_ID).state == "armed_away"


async def test_concurrent_mode_requests_are_serialized(hass, mock_client):
    _, writes = console(mock_client)
    await setup_entry(hass, mock_config_entry(MAPPING))
    await asyncio.gather(call(hass, "away"), call(hass, "night"))
    assert writes == [
        ("disarm", HOME_ID),
        ("arm", AWAY_ID),
        ("disarm", AWAY_ID),
        ("arm", OTHER_ID),
    ]
    assert hass.states.get(ENTITY_ID).state == "armed_night"
