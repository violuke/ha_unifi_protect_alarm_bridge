"""Tests for the alarm control panel entity."""

from homeassistant.components.alarm_control_panel import (
    DOMAIN as ALARM_DOMAIN,
    AlarmControlPanelEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
import pytest

from custom_components.unifi_protect_alarm_bridge.api import CannotConnect
from custom_components.unifi_protect_alarm_bridge.const import (
    CONF_PROFILE_AWAY,
    CONF_PROFILE_HOME,
    DOMAIN,
)

from .conftest import setup_entry
from .helpers import (
    AWAY_ID,
    ENTITY_ID,
    HOME_ID,
    OTHER_ID,
    UNIQUE_ID,
    make_profile,
    mock_config_entry,
)

TWO_MODES = {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: HOME_ID}


async def _call(hass, service: str) -> None:
    await hass.services.async_call(
        ALARM_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("disarmed", "disarmed"),
        ("arming", "arming"),
        ("armed", "armed_away"),
        ("breached", "triggered"),
        ("some_future_state", STATE_UNKNOWN),
    ],
)
async def test_state_mapping(hass, mock_client, state, expected) -> None:
    mock_client.async_get_profiles.return_value = [make_profile(state=state)]
    await setup_entry(hass, mock_config_entry())
    assert hass.states.get(ENTITY_ID).state == expected


async def test_armed_home_profile(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(),
        make_profile(profile_id=HOME_ID, state="armed"),
    ]
    await setup_entry(hass, mock_config_entry(TWO_MODES))
    assert hass.states.get(ENTITY_ID).state == "armed_home"


async def test_unmapped_profile_armed_elsewhere(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(),
        make_profile(profile_id=OTHER_ID, state="armed"),
    ]
    await setup_entry(hass, mock_config_entry())
    state = hass.states.get(ENTITY_ID)
    assert state.state == "armed_custom_bypass"
    assert state.attributes["profile_title"] == "Other"


async def test_most_severe_profile_wins(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(state="armed"),
        make_profile(profile_id=HOME_ID, state="breached"),
    ]
    await setup_entry(hass, mock_config_entry(TWO_MODES))
    state = hass.states.get(ENTITY_ID)
    assert state.state == "triggered"
    assert state.attributes["profile_title"] == "Home"


async def test_push_update_changes_state(hass, mock_client) -> None:
    entry = mock_config_entry()
    await setup_entry(hass, entry)

    entry.runtime_data.handle_profile(make_profile(state="breached"))
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == "triggered"


async def test_supported_features_follow_mapping(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(),
        make_profile(profile_id=HOME_ID),
    ]
    await setup_entry(hass, mock_config_entry(TWO_MODES))
    features = hass.states.get(ENTITY_ID).attributes[ATTR_SUPPORTED_FEATURES]
    assert features == (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
    )


async def test_attributes_and_device(hass, mock_client) -> None:
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    attributes = hass.states.get(ENTITY_ID).attributes
    assert attributes["profile_title"] == "Away"
    assert attributes["activation_delay"] == 60
    assert attributes["state_set_at"] == "2026-09-27T13:55:44+00:00"
    assert attributes["code_arm_required"] is False

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, UNIQUE_ID), entry.entry_id
    )
    assert device.configuration_url == "https://192.0.2.1"
    assert device.sw_version == "7.2.105"
    assert device.model == "UDM-PRO"


async def test_arm_away_uses_the_response(hass, mock_client) -> None:
    await setup_entry(hass, mock_config_entry())
    polls = mock_client.async_get_profiles.await_count

    await _call(hass, "alarm_arm_away")

    mock_client.async_arm.assert_awaited_once_with(AWAY_ID)
    assert hass.states.get(ENTITY_ID).state == "arming"
    assert mock_client.async_get_profiles.await_count == polls


async def test_arm_with_no_exit_delay_is_immediately_armed(hass, mock_client) -> None:
    mock_client.async_arm.side_effect = lambda pid: make_profile(
        profile_id=pid, state="armed", activation_delay=None
    )
    await setup_entry(hass, mock_config_entry())

    await _call(hass, "alarm_arm_away")

    assert hass.states.get(ENTITY_ID).state == "armed_away"


@pytest.mark.parametrize("state", ["arming", "armed"])
async def test_arm_again_while_active_is_a_no_op(hass, mock_client, state) -> None:
    mock_client.async_get_profiles.return_value = [make_profile(state=state)]
    await setup_entry(hass, mock_config_entry())
    assert hass.states.get(ENTITY_ID) is not None  # else the call is a silent no-op

    await _call(hass, "alarm_arm_away")

    mock_client.async_arm.assert_not_awaited()


async def test_arm_disarms_other_active_profiles_first(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(),
        make_profile(profile_id=HOME_ID, state="armed"),
    ]
    await setup_entry(hass, mock_config_entry(TWO_MODES))

    await _call(hass, "alarm_arm_away")

    mock_client.async_disarm.assert_awaited_once_with(HOME_ID)
    mock_client.async_arm.assert_awaited_once_with(AWAY_ID)
    assert hass.states.get(ENTITY_ID).state == "arming"


async def test_arm_missing_profile_raises(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [make_profile(profile_id=HOME_ID)]
    await setup_entry(hass, mock_config_entry())

    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "alarm_arm_away")

    assert err.value.translation_key == "profile_missing"


async def test_arm_failure_raises(hass, mock_client) -> None:
    mock_client.async_arm.side_effect = CannotConnect("boom")
    await setup_entry(hass, mock_config_entry())

    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "alarm_arm_away")

    assert err.value.translation_key == "arm_failed"


async def test_disarm_when_already_disarmed_is_a_no_op(hass, mock_client) -> None:
    await setup_entry(hass, mock_config_entry())
    assert hass.states.get(ENTITY_ID).state == "disarmed"

    await _call(hass, "alarm_disarm")

    mock_client.async_disarm.assert_not_awaited()


async def test_disarm_all_reports_partial_failure(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(state="armed"),
        make_profile(profile_id=HOME_ID, state="armed"),
    ]

    def disarm(profile_id: str):
        if profile_id == AWAY_ID:
            raise CannotConnect("boom")
        return make_profile(profile_id=profile_id, state="disarmed")

    mock_client.async_disarm.side_effect = disarm
    entry = mock_config_entry(TWO_MODES)
    await setup_entry(hass, entry)

    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "alarm_disarm")

    assert err.value.translation_key == "disarm_failed"
    assert err.value.translation_placeholders["profile"] == "Away"
    assert mock_client.async_disarm.await_count == 2
    assert entry.runtime_data.data[HOME_ID].state == "disarmed"
