"""Offline regressions for stale status and unintended control side effects."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.unifi_protect_alarm_bridge.api import CannotConnect, RateLimited

from .conftest import setup_entry
from .helpers import AWAY_ID, ENTITY_ID, HOME_ID, make_profile, mock_config_entry


async def call(hass, service):
    await hass.services.async_call(
        "alarm_control_panel", service, {"entity_id": ENTITY_ID}, blocking=True
    )


@pytest.mark.parametrize("newer_timestamp", [True, False])
async def test_late_arm_response_cannot_replace_fob_disarm(
    hass, mock_client, newer_timestamp
):
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    older = make_profile(state="armed")
    newer = replace(
        older,
        state="disarmed",
        state_set_at=older.state_set_at + timedelta(seconds=int(newer_timestamp)),
    )

    async def slow_arm(_profile_id):
        entry.runtime_data.handle_profile(newer)
        mock_client.async_get_profiles.return_value = [newer]
        return older

    mock_client.async_arm.side_effect = slow_arm
    await call(hass, "alarm_arm_away")
    assert hass.states.get(ENTITY_ID).state == "disarmed"
    assert entry.runtime_data.data[AWAY_ID] == newer


async def test_older_push_cannot_replace_newer_status(hass, mock_client):
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    old = make_profile(state="disarmed")
    new = replace(
        old, state="armed", state_set_at=old.state_set_at + timedelta(seconds=2)
    )
    entry.runtime_data.handle_profile(new)
    entry.runtime_data.handle_profile(old)
    assert hass.states.get(ENTITY_ID).state == "armed_away"


async def test_auth_failure_cannot_be_hidden_by_partial_push(hass, mock_client):
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    with patch.object(entry, "async_start_reauth"):
        coordinator.handle_auth_failed()
    assert hass.states.get(ENTITY_ID).state == "unavailable"
    mock_client.async_get_profiles.side_effect = CannotConnect("still offline")
    coordinator.handle_profile(make_profile(state="armed"))
    assert hass.states.get(ENTITY_ID).state == "unavailable"
    mock_client.async_get_profiles.side_effect = None
    mock_client.async_get_profiles.return_value = [make_profile(state="armed")]
    await coordinator.async_refresh()
    assert hass.states.get(ENTITY_ID).state == "armed_away"


async def test_pushes_do_not_postpone_polling_and_unload_stops_it(hass, mock_client):
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    with patch.object(
        coordinator, "async_request_refresh", new_callable=AsyncMock
    ) as poll:
        coordinator.handle_profile(make_profile())
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done()
        poll.assert_awaited()
        await hass.config_entries.async_unload(entry.entry_id)
        poll.reset_mock()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=25))
        await hass.async_block_till_done()
        poll.assert_not_called()


async def test_rate_limit_backoff_applies_to_independent_timer(hass, mock_client):
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    mock_client.async_get_profiles.side_effect = RateLimited("slow down")
    coordinator = entry.runtime_data
    await coordinator.async_refresh()
    with patch.object(
        coordinator, "async_request_refresh", new_callable=AsyncMock
    ) as poll:
        await coordinator._poll_tick(dt_util.utcnow())
        poll.assert_not_called()


async def test_unchanged_profiles_still_publish_poll_freshness(
    hass, mock_client, freezer
):
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    before = hass.states.get(ENTITY_ID)
    freezer.tick(timedelta(seconds=15))
    await entry.runtime_data.async_refresh()
    after = hass.states.get(ENTITY_ID)
    assert after.state == before.state == "disarmed"
    assert (
        after.attributes["last_successful_poll"]
        != before.attributes["last_successful_poll"]
    )


@pytest.mark.parametrize("bad_response", ["wrong-id", "wrong-state"])
async def test_invalid_action_acknowledgement_is_not_reported_as_success(
    hass, mock_client, bad_response
):
    await setup_entry(hass, mock_config_entry())

    async def bad_ack(_profile_id):
        mock_client.async_get_profiles.side_effect = CannotConnect("still offline")
        return make_profile(
            profile_id=HOME_ID if bad_response == "wrong-id" else AWAY_ID,
            state="disarmed" if bad_response == "wrong-state" else "armed",
        )

    mock_client.async_arm.side_effect = bad_ack
    with pytest.raises(HomeAssistantError):
        await call(hass, "alarm_arm_away")
    assert hass.states.get(ENTITY_ID).state == "unavailable"
