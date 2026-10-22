"""Regressions for observations arriving while a request is in flight."""

import asyncio
from datetime import timedelta
from unittest.mock import patch

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.unifi_protect_alarm_bridge.api import RateLimited

from .conftest import setup_entry
from .helpers import AWAY_ID, HOME_ID, make_profile, mock_config_entry


@pytest.mark.parametrize("deleted", [False, True])
async def test_late_action_does_not_overwrite_a_newer_poll(hass, mock_client, deleted):
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    started, release = asyncio.Event(), asyncio.Event()

    async def arm(_pid):
        started.set()
        await release.wait()
        return make_profile(state="armed")

    mock_client.async_arm.side_effect = arm
    task = hass.async_create_task(coordinator.async_arm_profile(AWAY_ID))
    await started.wait()
    if deleted:
        mock_client.async_get_profiles.return_value = [make_profile(profile_id=HOME_ID)]
    await coordinator.async_refresh()
    with patch.object(coordinator, "handle_resync") as resync:
        release.set()
        await task
    if deleted:
        assert AWAY_ID not in coordinator.data
    else:
        assert coordinator.data[AWAY_ID].state == "disarmed"
    resync.assert_called_once()


async def test_poll_preserves_a_profile_added_during_its_request(hass, mock_client):
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    started, release = asyncio.Event(), asyncio.Event()

    async def poll():
        started.set()
        await release.wait()
        return [make_profile()]

    mock_client.async_get_profiles.side_effect = poll
    task = hass.async_create_task(coordinator.async_refresh())
    await started.wait()
    coordinator.handle_profile(make_profile(profile_id=HOME_ID, state="armed"))
    release.set()
    await task
    assert coordinator.data[HOME_ID].state == "armed"


async def test_direct_refresh_respects_rate_limit_backoff(hass, mock_client, freezer):
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    mock_client.async_get_profiles.side_effect = RateLimited("slow down")
    await coordinator.async_refresh()
    reads = mock_client.async_get_profiles.await_count
    await coordinator.async_refresh()
    assert mock_client.async_get_profiles.await_count == reads
    freezer.tick(timedelta(seconds=301))
    mock_client.async_get_profiles.side_effect = None
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert mock_client.async_get_profiles.await_count == reads + 1


async def test_rate_limited_action_does_not_trigger_an_immediate_read(
    hass, mock_client
):
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    mock_client.async_arm.side_effect = RateLimited("slow down")
    reads = mock_client.async_get_profiles.await_count
    with pytest.raises(HomeAssistantError):
        await entry.runtime_data.async_arm_profile(AWAY_ID)
    await hass.async_block_till_done()
    assert mock_client.async_get_profiles.await_count == reads
