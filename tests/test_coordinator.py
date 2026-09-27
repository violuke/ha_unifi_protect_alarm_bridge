"""Tests for the arm profile coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock, create_autospec

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

# Importing config_flow registers the reauth handler, as HA's integration preload
# does at runtime. Without it, a poll's ConfigEntryAuthFailed can't start reauth
# (async_start_reauth_if_available) when this file runs on its own.
from custom_components.unifi_protect_alarm_bridge import config_flow  # noqa: F401
from custom_components.unifi_protect_alarm_bridge.api import (
    AuthFailed,
    CannotConnect,
    InsufficientPermissions,
    MfaRequired,
    UnexpectedResponse,
    UniFiAlarmClient,
)
from custom_components.unifi_protect_alarm_bridge.const import (
    DOMAIN,
    ISSUE_API_CHANGED,
    ISSUE_GLOBAL_MODE_OFF,
    ISSUE_NOT_SUPER_ADMIN,
    ISSUE_PROFILE_MISSING,
    ISSUE_PUSH_UNAVAILABLE,
    POLL_INTERVAL_PUSH_DOWN,
    POLL_INTERVAL_PUSH_HEALTHY,
    PROMOTION_MAX_OVERDUE,
)
from custom_components.unifi_protect_alarm_bridge.coordinator import (
    UniFiAlarmCoordinator,
    async_delete_entry_issues,
)

from .helpers import AWAY_ID, CONSOLE, HOME_ID, make_profile, mock_config_entry


@pytest.fixture
def client() -> MagicMock:
    client = create_autospec(UniFiAlarmClient, instance=True)
    client.async_get_profiles.return_value = [make_profile()]
    client.async_get_console_info.return_value = CONSOLE
    return client


@pytest.fixture
async def coordinator(
    hass: HomeAssistant, client: MagicMock
) -> AsyncGenerator[UniFiAlarmCoordinator]:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    coordinator = UniFiAlarmCoordinator(hass, entry, client, CONSOLE)
    yield coordinator
    await coordinator.async_shutdown()


def _issue(hass: HomeAssistant, coordinator: UniFiAlarmCoordinator, key: str):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{key}_{coordinator.config_entry.entry_id}"
    )


async def test_poll_populates_data(coordinator) -> None:
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data == {AWAY_ID: make_profile()}
    assert coordinator.update_interval == POLL_INTERVAL_PUSH_DOWN
    assert coordinator.last_poll_success_at is not None


async def test_poll_started_before_a_push_does_not_overwrite_it(
    hass, coordinator, client
) -> None:
    await coordinator.async_refresh()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_poll():
        started.set()
        await release.wait()
        return [make_profile(state="disarmed")]

    client.async_get_profiles.side_effect = slow_poll
    task = hass.async_create_task(coordinator.async_refresh())
    await started.wait()

    coordinator.handle_profile(make_profile(state="arming"))
    release.set()
    await task

    assert coordinator.data[AWAY_ID].state == "arming"


async def test_poll_started_after_a_push_wins(coordinator, client) -> None:
    await coordinator.async_refresh()
    coordinator.handle_profile(make_profile(state="arming"))

    client.async_get_profiles.return_value = [make_profile(state="armed")]
    await coordinator.async_refresh()

    assert coordinator.data[AWAY_ID].state == "armed"


async def test_poll_applies_new_and_deleted_profiles(coordinator, client) -> None:
    await coordinator.async_refresh()
    coordinator.handle_profile(make_profile(state="arming"))

    client.async_get_profiles.return_value = [make_profile(profile_id=HOME_ID)]
    await coordinator.async_refresh()

    assert set(coordinator.data) == {HOME_ID}


async def test_connection_change_switches_interval_and_resyncs(
    hass, coordinator, client
) -> None:
    await coordinator.async_refresh()

    coordinator.handle_connection_change(True)
    await hass.async_block_till_done()
    assert coordinator.update_interval == POLL_INTERVAL_PUSH_HEALTHY
    assert client.async_get_profiles.await_count == 2

    coordinator.handle_connection_change(False)
    await hass.async_block_till_done()
    assert coordinator.update_interval == POLL_INTERVAL_PUSH_DOWN


@pytest.mark.parametrize("error", [AuthFailed("x"), MfaRequired("x")])
async def test_auth_failure_starts_reauth(hass, coordinator, client, error) -> None:
    client.async_get_profiles.side_effect = error
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_listener_auth_failure_starts_reauth(hass, coordinator) -> None:
    coordinator.handle_auth_failed()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_not_super_admin_issue_lifecycle(hass, coordinator, client) -> None:
    client.async_get_profiles.side_effect = InsufficientPermissions("x")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert _issue(hass, coordinator, ISSUE_NOT_SUPER_ADMIN) is not None

    client.async_get_profiles.side_effect = None
    await coordinator.async_refresh()
    assert _issue(hass, coordinator, ISSUE_NOT_SUPER_ADMIN) is None


async def test_unexpected_response_raises_api_changed(
    hass, coordinator, client
) -> None:
    client.async_get_profiles.side_effect = UnexpectedResponse("x", {"weird": 1})
    await coordinator.async_refresh()

    issue = _issue(hass, coordinator, ISSUE_API_CHANGED)
    assert issue is not None
    assert issue.translation_placeholders["protect_version"] == "7.2.105"
    assert coordinator.last_unexpected_payload == {"weird": 1}
    assert _issue(hass, coordinator, ISSUE_GLOBAL_MODE_OFF) is None

    client.async_get_profiles.side_effect = None
    await coordinator.async_refresh()
    assert _issue(hass, coordinator, ISSUE_API_CHANGED) is None


async def test_unexpected_response_in_local_mode_raises_global_mode_off(
    hass, coordinator, client
) -> None:
    client.async_get_profiles.side_effect = UnexpectedResponse("x")
    client.async_get_console_info.return_value = replace(
        CONSOLE, external_alarm_manager=False
    )
    await coordinator.async_refresh()

    assert _issue(hass, coordinator, ISSUE_GLOBAL_MODE_OFF) is not None
    assert _issue(hass, coordinator, ISSUE_API_CHANGED) is None


async def test_empty_profiles_in_local_mode_fails(hass, coordinator, client) -> None:
    client.async_get_profiles.return_value = []
    client.async_get_console_info.return_value = replace(
        CONSOLE, external_alarm_manager=False
    )
    await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert _issue(hass, coordinator, ISSUE_GLOBAL_MODE_OFF) is not None


async def test_cannot_connect_marks_update_failed(coordinator, client) -> None:
    client.async_get_profiles.side_effect = CannotConnect("x")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success


async def test_profile_missing_issue_lifecycle(hass, coordinator, client) -> None:
    client.async_get_profiles.return_value = [make_profile(profile_id=HOME_ID)]
    await coordinator.async_refresh()
    issue = _issue(hass, coordinator, ISSUE_PROFILE_MISSING)
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": coordinator.config_entry.entry_id}

    client.async_get_profiles.return_value = [make_profile()]
    await coordinator.async_refresh()
    assert _issue(hass, coordinator, ISSUE_PROFILE_MISSING) is None


async def test_push_unavailable_issue_lifecycle(hass, coordinator, freezer) -> None:
    await coordinator.async_refresh()
    assert _issue(hass, coordinator, ISSUE_PUSH_UNAVAILABLE) is None

    freezer.tick(timedelta(hours=1, seconds=1))
    await coordinator.async_refresh()
    assert _issue(hass, coordinator, ISSUE_PUSH_UNAVAILABLE) is not None

    coordinator.handle_connection_change(True)
    await hass.async_block_till_done()
    assert _issue(hass, coordinator, ISSUE_PUSH_UNAVAILABLE) is None


async def test_promotion_refresh_fires_when_the_exit_delay_ends(
    hass, coordinator, client
) -> None:
    due = dt_util.utcnow() + timedelta(seconds=60)
    client.async_get_profiles.return_value = [
        make_profile(state="arming", promotion_due=due)
    ]
    await coordinator.async_refresh()

    client.async_get_profiles.return_value = [make_profile(state="armed")]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=63))
    await hass.async_block_till_done()

    assert coordinator.data[AWAY_ID].state == "armed"
    assert client.async_get_profiles.await_count == 2


async def test_promotion_refresh_gives_up_if_the_console_never_promotes(
    hass, coordinator, client
) -> None:
    """Clock skew: the due time is already past and the state stays 'arming'."""
    past = dt_util.utcnow() - timedelta(seconds=30)
    client.async_get_profiles.return_value = [
        make_profile(state="arming", promotion_due=past)
    ]
    await coordinator.async_refresh()

    for step in range(1, 7):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=15 * step))
        await hass.async_block_till_done()

    assert client.async_get_profiles.await_count == 1 + PROMOTION_MAX_OVERDUE


async def test_no_promotion_timer_when_arming_is_instant(coordinator, client) -> None:
    """Activation delay off: arm returns 'armed' with no due time."""
    await coordinator.async_refresh()
    client.async_arm.return_value = make_profile(state="armed", activation_delay=None)

    await coordinator.async_arm_profile(AWAY_ID)

    assert coordinator.data[AWAY_ID].state == "armed"
    assert coordinator._promotion_unsub is None


async def test_arm_applies_the_response_without_polling(coordinator, client) -> None:
    await coordinator.async_refresh()
    client.async_arm.return_value = make_profile(state="arming")

    await coordinator.async_arm_profile(AWAY_ID)

    assert coordinator.data[AWAY_ID].state == "arming"
    assert client.async_get_profiles.await_count == 1


async def test_action_errors_are_translated(coordinator, client) -> None:
    await coordinator.async_refresh()
    client.async_arm.side_effect = CannotConnect("boom")

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_arm_profile(AWAY_ID)

    assert err.value.translation_key == "arm_failed"
    assert err.value.translation_placeholders == {"profile": "Away", "error": "boom"}


async def test_action_auth_failure_starts_reauth(hass, coordinator, client) -> None:
    await coordinator.async_refresh()
    client.async_disarm.side_effect = AuthFailed("x")

    with pytest.raises(HomeAssistantError):
        await coordinator.async_disarm_profile(AWAY_ID)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_shutdown_cancels_the_promotion_timer(coordinator, client) -> None:
    due = dt_util.utcnow() + timedelta(seconds=60)
    client.async_get_profiles.return_value = [
        make_profile(state="arming", promotion_due=due)
    ]
    await coordinator.async_refresh()
    assert coordinator._promotion_unsub is not None

    await coordinator.async_shutdown()

    assert coordinator._promotion_unsub is None


async def test_push_after_shutdown_is_ignored(coordinator) -> None:
    await coordinator.async_refresh()
    await coordinator.async_shutdown()

    due = dt_util.utcnow() + timedelta(seconds=60)
    coordinator.handle_profile(make_profile(state="arming", promotion_due=due))

    assert coordinator.data[AWAY_ID].state == "disarmed"
    assert coordinator._promotion_unsub is None


async def test_delete_entry_issues(hass, coordinator, client) -> None:
    client.async_get_profiles.side_effect = InsufficientPermissions("x")
    await coordinator.async_refresh()

    async_delete_entry_issues(hass, coordinator.config_entry.entry_id)

    assert _issue(hass, coordinator, ISSUE_NOT_SUPER_ADMIN) is None
