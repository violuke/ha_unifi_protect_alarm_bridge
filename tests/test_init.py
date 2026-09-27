"""Tests for setting up, unloading and removing the integration."""

import asyncio
from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest

from custom_components.unifi_protect_alarm_bridge.api import (
    AuthFailed,
    CannotConnect,
    MfaRequired,
    RateLimited,
    UnexpectedResponse,
)
from custom_components.unifi_protect_alarm_bridge.const import (
    DOMAIN,
    ISSUE_API_CHANGED,
)

from .conftest import setup_entry
from .helpers import ENTITY_ID, HOME_ID, make_profile, mock_config_entry


async def test_setup_unload(hass, mock_client, mock_listener_run) -> None:
    entry = mock_config_entry()
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data is not None
    mock_listener_run.assert_awaited_once()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_unload_cancels_socket_task_and_timers(
    hass, mock_client, mock_listener_run
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def run_forever() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    mock_listener_run.side_effect = run_forever
    due = dt_util.utcnow() + timedelta(seconds=60)
    mock_client.async_get_profiles.return_value = [
        make_profile(state="arming", promotion_due=due)
    ]
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    await started.wait()
    coordinator = entry.runtime_data
    assert coordinator._promotion_unsub is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert cancelled.is_set()
    assert coordinator._promotion_unsub is None


@pytest.mark.skip(reason="entity lands in Task 7")
@pytest.mark.parametrize(
    ("state", "expected"),
    [("armed", "armed_away"), ("breached", "triggered"), ("arming", "arming")],
)
async def test_restart_while_active_shows_the_real_state(
    hass, mock_client, state, expected
) -> None:
    mock_client.async_get_profiles.return_value = [make_profile(state=state)]
    await setup_entry(hass, mock_config_entry())
    assert hass.states.get(ENTITY_ID).state == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CannotConnect("x"), ConfigEntryState.SETUP_RETRY),
        (RateLimited("x"), ConfigEntryState.SETUP_RETRY),
        (UnexpectedResponse("x"), ConfigEntryState.SETUP_RETRY),
        (AuthFailed("x"), ConfigEntryState.SETUP_ERROR),
        (MfaRequired("x"), ConfigEntryState.SETUP_ERROR),
    ],
)
async def test_console_info_errors(hass, mock_client, error, expected) -> None:
    mock_client.async_get_console_info.side_effect = error
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    assert entry.state is expected


async def test_auth_failure_during_setup_starts_reauth(hass, mock_client) -> None:
    mock_client.async_get_console_info.side_effect = AuthFailed("x")
    await setup_entry(hass, mock_config_entry())
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_first_refresh_failure_retries(hass, mock_client) -> None:
    mock_client.async_get_profiles.side_effect = CannotConnect("x")
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_missing_profile_still_loads(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [make_profile(profile_id=HOME_ID)]
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED


async def test_remove_entry_deletes_issues(hass, mock_client) -> None:
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    issue_id = f"{ISSUE_API_CHANGED}_{entry.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_API_CHANGED,
        translation_placeholders={"protect_version": "x", "issue_url": "x"},
    )

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
