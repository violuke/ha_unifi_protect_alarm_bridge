"""Existing-session reuse without reading or storing a second password."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_protect_alarm_bridge.api import (
    CannotConnect,
    InsufficientPermissions,
    UnexpectedResponse,
)
from custom_components.unifi_protect_alarm_bridge.const import (
    CONF_CERTIFICATE_FINGERPRINT,
    CONF_PROFILE_AWAY,
    CONF_SOURCE_ENTRY_ID,
    DOMAIN,
)
from custom_components.unifi_protect_alarm_bridge.shared import SharedProtectClient

from .conftest import setup_entry
from .helpers import (
    AWAY_ID,
    CONSOLE,
    CONSOLE_JSON,
    ENTITY_ID,
    UNIQUE_ID,
    make_profile,
    profile_json,
)

BASE = "custom_components.unifi_protect_alarm_bridge"


class NoCredentialAccess:
    def __getitem__(self, key):
        raise AssertionError("Stored credentials must never be accessed")

    def get(self, *args):
        raise AssertionError("Stored credentials must never be accessed")


@pytest.fixture
def shared():
    response = SimpleNamespace(status=200, json=AsyncMock(return_value=CONSOLE_JSON))
    session = MagicMock()
    session.request.return_value.__aenter__.return_value = response
    api = SimpleNamespace(
        base_url="https://192.0.2.1",
        is_public_only=False,
        is_authenticated=MagicMock(return_value=True),
        get_session=AsyncMock(return_value=session),
        headers={"X-CSRF-Token": "fake-csrf"},
        authenticate=AsyncMock(),
        clear_session=AsyncMock(),
        async_disconnect_ws=AsyncMock(),
    )
    source = SimpleNamespace(
        domain="unifiprotect",
        state=ConfigEntryState.LOADED,
        runtime_data=SimpleNamespace(api=api),
        data=NoCredentialAccess(),
    )
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=MagicMock(return_value=source))
    )
    client = SharedProtectClient(hass, "fake-source", "ab" * 32)
    return SimpleNamespace(
        client=client,
        api=api,
        source=source,
        hass=hass,
        session=session,
        response=response,
    )


async def test_reads_with_existing_session_never_password_or_login(shared):
    assert (await shared.client.async_get_console_info()) == CONSOLE
    shared.response.json.return_value = [profile_json(state="armed")]
    assert (await shared.client.async_get_profiles())[0].state == "armed"
    call = shared.session.request.call_args
    assert call.args == ("GET", "https://192.0.2.1/api/v2/alarms/profiles")
    assert call.kwargs["ssl"].fingerprint == bytes.fromhex("ab" * 32)
    assert call.kwargs["allow_redirects"] is False
    assert call.kwargs["headers"] == shared.api.headers
    shared.api.authenticate.assert_not_called()
    shared.api.clear_session.assert_not_called()
    shared.session.close.assert_not_called()


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v2/alarms/profiles/example/actions/disarm"),
        ("POST", "/api/auth/login"),
        ("GET", "/proxy/protect/api/cameras"),
    ],
)
async def test_shared_guard_blocks_before_accessing_source(shared, method, path):
    with pytest.raises(UnexpectedResponse):
        await shared.client._request(method, path)
    shared.hass.config_entries.async_get_entry.assert_not_called()
    shared.session.request.assert_not_called()


@pytest.mark.parametrize(
    "failure", ["unloaded", "removed", "public_only", "expired", "insecure_origin"]
)
async def test_unready_source_is_unavailable_not_reauthenticated(shared, failure):
    if failure == "unloaded":
        shared.source.state = ConfigEntryState.NOT_LOADED
    if failure == "removed":
        shared.hass.config_entries.async_get_entry.return_value = None
    if failure == "public_only":
        shared.api.is_public_only = True
    if failure == "expired":
        shared.api.is_authenticated.return_value = False
    if failure == "insecure_origin":
        shared.api.base_url = "http://192.0.2.1"
    with pytest.raises(CannotConnect):
        await shared.client.async_get_console_info()
    shared.session.request.assert_not_called()
    shared.api.authenticate.assert_not_called()


@pytest.mark.parametrize(
    "status,error", [(401, CannotConnect), (403, InsufficientPermissions)]
)
async def test_session_errors_do_not_change_official_login(shared, status, error):
    shared.response.status = status
    with pytest.raises(error):
        await shared.client.async_get_profiles()
    shared.api.authenticate.assert_not_called()
    shared.api.clear_session.assert_not_called()
    shared.response.status = 200
    shared.response.json.return_value = [profile_json(state="armed")]
    assert (await shared.client.async_get_profiles())[0].state == "armed"


async def test_source_reload_uses_replacement_session(shared):
    await shared.client.async_get_console_info()
    replacement = MagicMock()
    replacement.request.return_value.__aenter__.return_value = shared.response
    new_api = SimpleNamespace(
        **{**vars(shared.api), "get_session": AsyncMock(return_value=replacement)}
    )
    shared.source.runtime_data.api = new_api
    await shared.client.async_get_console_info()
    replacement.request.assert_called_once()
    assert shared.session.request.call_count == 1


@pytest.mark.parametrize("action,state", [("arm", "armed"), ("disarm", "disarmed")])
async def test_controls_use_discovered_profiles_and_current_headers(
    shared, action, state
):
    shared.response.json.return_value = [profile_json()]
    await shared.client.async_get_profiles()
    shared.api.headers = {"X-CSRF-Token": "fake-refreshed-session"}
    shared.response.json.return_value = profile_json(state=state)
    result = await getattr(shared.client, f"async_{action}")(AWAY_ID)
    assert result.state == state
    call = shared.session.request.call_args
    assert call.args == (
        "POST",
        f"https://192.0.2.1/api/v2/alarms/profiles/{AWAY_ID}/actions/{action}",
    )
    assert call.kwargs["headers"] == {"X-CSRF-Token": "fake-refreshed-session"}
    assert call.kwargs["allow_redirects"] is False
    assert call.kwargs["ssl"].fingerprint == bytes.fromhex("ab" * 32)
    shared.api.authenticate.assert_not_called()


async def test_removed_profile_cannot_receive_commands(shared):
    shared.response.json.return_value = [profile_json()]
    await shared.client.async_get_profiles()
    shared.response.json.return_value = []
    await shared.client.async_get_profiles()
    shared.session.request.reset_mock()
    with pytest.raises(UnexpectedResponse):
        await shared.client.async_arm(AWAY_ID)
    shared.session.request.assert_not_called()


async def test_failed_write_is_never_retried(shared):
    shared.response.json.return_value = [profile_json()]
    await shared.client.async_get_profiles()
    shared.session.request.reset_mock()
    shared.session.request.return_value.__aenter__.side_effect = TimeoutError()
    with pytest.raises(CannotConnect):
        await shared.client.async_arm(AWAY_ID)
    assert shared.session.request.call_count == 1
    shared.api.authenticate.assert_not_called()


async def test_setup_poll_only_and_unload_does_not_close_shared_session(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Trial",
        unique_id=UNIQUE_ID,
        data={CONF_SOURCE_ENTRY_ID: "fake-source"},
        options={CONF_PROFILE_AWAY: AWAY_ID},
    )
    with patch(f"{BASE}.SharedProtectClient", autospec=True) as client_cls:
        client_cls.return_value.async_get_console_info.return_value = CONSOLE
        client_cls.return_value.async_get_profiles.return_value = [
            make_profile(state="armed")
        ]
        await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.listener is None
        state = hass.states.get(ENTITY_ID)
        assert state.state == "armed_away"
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not hasattr(client_cls.return_value, "async_login")
        assert not hasattr(client_cls.return_value, "close")


async def test_existing_setup_has_no_password_field_or_saved_password(hass):
    source = MockConfigEntry(domain="unifiprotect", title="Already connected", data={})
    source.add_to_hass(hass)
    with (
        patch(f"{BASE}.config_flow.SharedProtectClient", autospec=True) as client_cls,
        patch(f"{BASE}.async_setup_entry", return_value=True),
    ):
        client_cls.return_value.async_get_console_info.return_value = CONSOLE
        client_cls.return_value.async_get_profiles.return_value = [make_profile()]
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] is FlowResultType.MENU
        assert result["menu_options"] == ["existing", "manual"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "existing"}
        )
        assert result["step_id"] == "existing"
        assert "password" not in repr(result["data_schema"])
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_SOURCE_ENTRY_ID: source.entry_id,
                CONF_CERTIFICATE_FINGERPRINT: "ab" * 32,
            },
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"] == {
            CONF_SOURCE_ENTRY_ID: source.entry_id,
            CONF_CERTIFICATE_FINGERPRINT: "ab" * 32,
        }


async def test_older_source_api_without_public_only_flag(shared):
    del shared.api.is_public_only
    assert await shared.client.async_get_console_info() == CONSOLE


async def test_source_runtime_not_ready_is_a_connection_error(shared):
    shared.source.runtime_data = SimpleNamespace()
    with pytest.raises(CannotConnect):
        await shared.client.async_get_console_info()
    shared.session.request.assert_not_called()


async def test_dedicated_account_remains_available_with_an_existing_source(hass):
    source = MockConfigEntry(domain="unifiprotect", title="Existing", data={})
    source.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"
    assert "password" in repr(result["data_schema"])


async def test_shared_reconfigure_prefills_current_source_and_pin(hass):
    for title in ("First console", "Selected console"):
        source = MockConfigEntry(domain="unifiprotect", title=title, data={})
        source.add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Alarm",
        unique_id=UNIQUE_ID,
        data={
            CONF_SOURCE_ENTRY_ID: source.entry_id,
            CONF_CERTIFICATE_FINGERPRINT: "ab" * 32,
        },
        options={CONF_PROFILE_AWAY: AWAY_ID},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    assert result["step_id"] == "existing"
    fields = {field.schema: field for field in result["data_schema"].schema}
    assert fields[CONF_SOURCE_ENTRY_ID].default() == source.entry_id
    assert (
        fields[CONF_CERTIFICATE_FINGERPRINT].description["suggested_value"] == "ab" * 32
    )
