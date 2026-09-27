"""Tests for the options flow (arm mode mapping)."""

from homeassistant.components.alarm_control_panel import AlarmControlPanelEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.unifi_protect_alarm_bridge.const import (
    CONF_PROFILE_AWAY,
    CONF_PROFILE_HOME,
)

from .conftest import setup_entry
from .helpers import AWAY_ID, ENTITY_ID, HOME_ID, make_profile, mock_config_entry


@pytest.mark.skip(reason="entity lands in Task 7")
async def test_options_flow_updates_mapping_and_reloads(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(),
        make_profile(profile_id=HOME_ID),
    ]
    entry = mock_config_entry()
    await setup_entry(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: AWAY_ID}
    )
    assert result["errors"] == {"base": "duplicate_profile"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: HOME_ID}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options == {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: HOME_ID}
    features = hass.states.get(ENTITY_ID).attributes[ATTR_SUPPORTED_FEATURES]
    assert features & AlarmControlPanelEntityFeature.ARM_HOME


async def test_options_flow_needs_a_loaded_entry(hass) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
