"""Tests for the profile_missing repair flow."""

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from custom_components.unifi_protect_alarm_bridge.const import (
    CONF_PROFILE_AWAY,
    CONF_PROFILE_HOME,
    DOMAIN,
    ISSUE_PROFILE_MISSING,
)
from custom_components.unifi_protect_alarm_bridge.repairs import async_create_fix_flow

from .conftest import setup_entry
from .helpers import HOME_ID, OTHER_ID, make_profile, mock_config_entry


async def _start(hass, issue_id: str, data):
    flow = await async_create_fix_flow(hass, issue_id, data)
    flow.hass = hass
    flow.flow_id = "test-flow"
    flow.handler = DOMAIN
    flow.issue_id = issue_id
    flow.data = data
    flow.context = {}
    return flow


async def test_profile_missing_fix_flow(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(profile_id=HOME_ID),
        make_profile(profile_id=OTHER_ID),
    ]
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    issue_id = f"{ISSUE_PROFILE_MISSING}_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None

    flow = await _start(hass, issue_id, issue.data)
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM

    result = await flow.async_step_init(
        {CONF_PROFILE_AWAY: HOME_ID, CONF_PROFILE_HOME: HOME_ID}
    )
    assert result["errors"] == {"base": "duplicate_profile"}

    result = await flow.async_step_init({CONF_PROFILE_AWAY: HOME_ID})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options == {CONF_PROFILE_AWAY: HOME_ID}
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_fix_flow_aborts_when_entry_not_loaded(hass) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    flow = await _start(hass, "profile_missing_x", {"entry_id": entry.entry_id})
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
