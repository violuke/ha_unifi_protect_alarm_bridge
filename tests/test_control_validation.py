"""Offline regressions for stale status and unintended control side effects."""

import asyncio
from dataclasses import replace

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.unifi_protect_alarm_bridge.api import CannotConnect
from custom_components.unifi_protect_alarm_bridge.const import (
    CONF_PROFILE_AWAY,
    CONF_PROFILE_HOME,
)

from .conftest import setup_entry
from .helpers import (
    AWAY_ID,
    CONSOLE,
    ENTITY_ID,
    HOME_ID,
    make_profile,
    mock_config_entry,
)


async def call(hass, service):
    await hass.services.async_call(
        "alarm_control_panel", service, {"entity_id": ENTITY_ID}, blocking=True
    )


async def test_rejected_breached_target_never_disarms_another_profile(
    hass, mock_client
):
    mock_client.async_get_profiles.return_value = [
        make_profile(state="breached"),
        make_profile(profile_id=HOME_ID, state="armed"),
    ]
    await setup_entry(
        hass,
        mock_config_entry({CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: HOME_ID}),
    )
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "alarm_arm_away")
    assert err.value.translation_key == "disarm_first"
    mock_client.async_arm.assert_not_awaited()
    mock_client.async_disarm.assert_not_awaited()


async def test_failed_mode_switch_reports_possible_disarm_without_retry(
    hass, mock_client
):
    mock_client.async_get_profiles.return_value = [
        make_profile(),
        make_profile(profile_id=HOME_ID, state="armed"),
    ]
    mock_client.async_arm.side_effect = CannotConnect("simulated failure")
    await setup_entry(
        hass,
        mock_config_entry({CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: HOME_ID}),
    )

    async def disarm(pid):
        mock_client.async_get_profiles.return_value = [
            make_profile(),
            make_profile(profile_id=HOME_ID),
        ]
        return make_profile(profile_id=pid)

    mock_client.async_disarm.side_effect = disarm
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "alarm_arm_away")
    assert err.value.translation_key == "mode_switch_failed"
    mock_client.async_arm.assert_awaited_once_with(AWAY_ID)
    mock_client.async_disarm.assert_awaited_once_with(HOME_ID)
    assert hass.states.get(ENTITY_ID).state == "disarmed"


async def test_disarm_refreshes_after_missed_external_arm(hass, mock_client):
    await setup_entry(hass, mock_config_entry())
    mock_client.async_get_profiles.return_value = [make_profile(state="armed")]
    await call(hass, "alarm_disarm")
    mock_client.async_disarm.assert_awaited_once_with(AWAY_ID)
    assert hass.states.get(ENTITY_ID).state == "disarmed"


@pytest.mark.parametrize("service", ["alarm_arm_away", "alarm_disarm"])
async def test_failed_fresh_read_blocks_commands(hass, mock_client, service):
    await setup_entry(hass, mock_config_entry())
    mock_client.async_get_profiles.side_effect = CannotConnect("offline")
    with pytest.raises(HomeAssistantError):
        await call(hass, service)
    mock_client.async_arm.assert_not_awaited()
    mock_client.async_disarm.assert_not_awaited()
    assert hass.states.get(ENTITY_ID).state == "unavailable"


async def test_unknown_profile_state_blocks_arming(hass, mock_client):
    mock_client.async_get_profiles.return_value = [make_profile(state="future-state")]
    await setup_entry(hass, mock_config_entry())
    with pytest.raises(HomeAssistantError):
        await call(hass, "alarm_arm_away")
    mock_client.async_arm.assert_not_awaited()
    mock_client.async_disarm.assert_not_awaited()


@pytest.mark.parametrize("service", ["alarm_arm_away", "alarm_disarm"])
@pytest.mark.parametrize("failure", ["identity", "local", "unknown", "offline"])
async def test_unverified_console_blocks_commands(hass, mock_client, service, failure):
    await setup_entry(hass, mock_config_entry())
    if failure == "offline":
        mock_client.async_get_console_info.side_effect = CannotConnect("offline")
    else:
        mock_client.async_get_console_info.return_value = replace(
            CONSOLE,
            mac="001122334455" if failure == "identity" else CONSOLE.mac,
            external_alarm_manager={"identity": True, "local": False, "unknown": None}[
                failure
            ],
        )
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, service)
    assert err.value.translation_key == "status_unavailable"
    mock_client.async_arm.assert_not_awaited()
    mock_client.async_disarm.assert_not_awaited()


async def test_concurrent_duplicate_arm_waits_for_fresh_state(hass, mock_client):
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    started, release = asyncio.Event(), asyncio.Event()

    async def arm(_profile_id):
        started.set()
        await release.wait()
        profile = make_profile(state="armed")
        mock_client.async_get_profiles.return_value = [profile]
        return profile

    mock_client.async_arm.side_effect = arm
    first = hass.async_create_task(call(hass, "alarm_arm_away"))
    await started.wait()
    second = hass.async_create_task(call(hass, "alarm_arm_away"))
    release.set()
    await asyncio.gather(first, second)
    mock_client.async_arm.assert_awaited_once_with(AWAY_ID)
    assert hass.states.get(ENTITY_ID).state == "armed_away"


async def test_control_uses_one_fresh_console_and_profile_read(hass, mock_client):
    await setup_entry(hass, mock_config_entry())
    mock_client.async_get_console_info.reset_mock()
    mock_client.async_get_profiles.reset_mock()
    await call(hass, "alarm_arm_away")
    mock_client.async_get_console_info.assert_awaited_once()
    mock_client.async_get_profiles.assert_awaited_once()
