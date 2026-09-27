"""Tests for the config flow."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.unifi_protect_alarm_bridge.api import (
    AuthFailed,
    CannotConnect,
    InsufficientPermissions,
    MfaRequired,
    RateLimited,
    UnexpectedResponse,
)
from custom_components.unifi_protect_alarm_bridge.config_flow import normalize_host
from custom_components.unifi_protect_alarm_bridge.const import (
    CONF_PROFILE_AWAY,
    CONF_PROFILE_HOME,
    DOMAIN,
)

from .helpers import (
    AWAY_ID,
    CONSOLE,
    ENTRY_DATA,
    HOME_ID,
    HOST,
    PASSWORD,
    UNIQUE_ID,
    USERNAME,
    make_profile,
    mock_config_entry,
)

USER_INPUT = {
    CONF_HOST: HOST,
    CONF_USERNAME: USERNAME,
    CONF_PASSWORD: PASSWORD,
    CONF_VERIFY_SSL: False,
}


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    with patch(
        "custom_components.unifi_protect_alarm_bridge.async_setup_entry",
        return_value=True,
    ) as mock:
        yield mock


@pytest.fixture
def flow_client() -> Generator[MagicMock]:
    with patch(
        "custom_components.unifi_protect_alarm_bridge.config_flow.UniFiAlarmClient",
        autospec=True,
    ) as client_cls:
        client = client_cls.return_value
        client.async_get_console_info.return_value = CONSOLE
        client.async_get_profiles.return_value = [make_profile()]
        yield client


async def _start_user_flow(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.0.2.1", "192.0.2.1"),
        ("  192.0.2.1  ", "192.0.2.1"),
        ("https://192.0.2.1/", "192.0.2.1"),
        ("https://192.0.2.1:8443/protect/dashboard", "192.0.2.1:8443"),
        ("console.local", "console.local"),
        ("fe80::1", "[fe80::1]"),
        ("[fe80::1]:443", "[fe80::1]:443"),
    ],
)
def test_normalize_host(raw: str, expected: str) -> None:
    assert normalize_host(raw) == expected


async def test_single_profile_creates_entry(hass, flow_client) -> None:
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test Console"
    assert result["data"] == ENTRY_DATA
    assert result["options"] == {CONF_PROFILE_AWAY: AWAY_ID}
    assert result["result"].unique_id == UNIQUE_ID


async def test_url_style_host_is_normalised(hass, flow_client) -> None:
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "https://192.0.2.1/"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.0.2.1"


async def test_multiple_profiles_ask_for_mapping(hass, flow_client) -> None:
    flow_client.async_get_profiles.return_value = [
        make_profile(),
        make_profile(profile_id=HOME_ID),
    ]
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "profiles"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: AWAY_ID}
    )
    assert result["errors"] == {"base": "duplicate_profile"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: HOME_ID}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: HOME_ID}


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (AuthFailed("x"), "invalid_auth"),
        (MfaRequired("x"), "mfa_not_supported"),
        (RateLimited("x"), "rate_limited"),
        (CannotConnect("x"), "cannot_connect"),
        (InsufficientPermissions("x"), "not_super_admin"),
        (UnexpectedResponse("x"), "unknown"),
        (RuntimeError("x"), "unknown"),
    ],
)
async def test_user_step_errors_then_recovers(hass, flow_client, error, key) -> None:
    flow_client.async_get_profiles.side_effect = error
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}

    flow_client.async_get_profiles.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_failed_attempt_does_not_echo_the_password(hass, flow_client) -> None:
    flow_client.async_get_profiles.side_effect = AuthFailed("x")
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    suggested = {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    }
    assert suggested[CONF_HOST] == HOST
    assert suggested[CONF_PASSWORD] is None


async def test_global_mode_off(hass, flow_client) -> None:
    flow_client.async_get_console_info.return_value = replace(
        CONSOLE, external_alarm_manager=False
    )
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "global_mode_off"}
    flow_client.async_get_profiles.assert_not_called()


async def test_no_profiles(hass, flow_client) -> None:
    flow_client.async_get_profiles.return_value = []
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "no_profiles"}


async def test_already_configured_updates_host(hass, flow_client) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "192.0.2.99"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.0.2.99"


async def test_reauth(hass, flow_client) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    flow_client.async_get_profiles.side_effect = AuthFailed("x")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    flow_client.async_get_profiles.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"


async def test_reauth_against_a_different_console_aborts(hass, flow_client) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    flow_client.async_get_console_info.return_value = replace(
        CONSOLE, mac="112233445566"
    )
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_PASSWORD] == PASSWORD


async def test_reconfigure(hass, flow_client) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "fe80::1", CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "[fe80::1]"
    assert entry.data[CONF_VERIFY_SSL] is True


async def test_reconfigure_to_a_different_console_aborts(hass, flow_client) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    flow_client.async_get_console_info.return_value = replace(
        CONSOLE, mac="112233445566"
    )
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.50", CONF_VERIFY_SSL: False}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_HOST] == HOST


async def test_unknown_global_mode_flag_does_not_block_setup(hass, flow_client) -> None:
    flow_client.async_get_console_info.return_value = replace(
        CONSOLE, external_alarm_manager=None
    )
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
