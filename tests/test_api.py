"""Tests for the Alarm Manager HTTP client, run against a real local server."""

import asyncio
from datetime import UTC, datetime

import aiohttp
import pytest

from custom_components.unifi_protect_alarm_bridge.api import (
    ArmProfile,
    AuthFailed,
    CannotConnect,
    ConsoleInfo,
    InsufficientPermissions,
    MfaRequired,
    RateLimited,
    UnexpectedResponse,
    UniFiAlarmClient,
)

from .fake_console import FakeConsole
from .helpers import AWAY_ID, PASSWORD, USERNAME, profile_json


async def test_get_profiles_parses_profiles(api_client, fake_console) -> None:
    due = datetime(2026, 9, 27, 13, 56, 44, tzinfo=UTC)
    fake_console.profiles = [profile_json(state="arming", promotion_due=due)]

    profiles = await api_client.async_get_profiles()

    assert profiles == [
        ArmProfile(
            id=AWAY_ID,
            title="Away",
            state="arming",
            activation_delay=60,
            state_set_at=datetime(2026, 9, 27, 13, 55, 44, tzinfo=UTC),
            state_promotion_due_at=due,
        )
    ]
    assert fake_console.login_count == 1


def test_parses_seven_digit_fractional_seconds() -> None:
    """The console sends 100ns precision, e.g. 13:56:44.5852528Z."""
    data = {**profile_json(), "state_promotion_due_at": "2026-09-27T13:56:44.5852528Z"}
    profile = ArmProfile.from_api(data)
    assert profile.state_promotion_due_at == datetime(
        2026, 9, 27, 13, 56, 44, 585252, tzinfo=UTC
    )


def test_as_dict_is_json_safe() -> None:
    profile = ArmProfile.from_api(profile_json())
    assert profile.as_dict() == {
        "id": AWAY_ID,
        "title": "Away",
        "state": "disarmed",
        "activation_delay": 60,
        "state_set_at": "2026-09-27T13:55:44+00:00",
        "state_promotion_due_at": None,
    }


async def test_default_cookie_jar_cannot_hold_an_ip_session(fake_console) -> None:
    """Documents why the integration must use aiohttp.CookieJar(unsafe=True)."""
    async with aiohttp.ClientSession() as session:
        client = UniFiAlarmClient(
            session, fake_console.host, USERNAME, PASSWORD, scheme="http"
        )
        with pytest.raises(UnexpectedResponse):
            await client.async_get_profiles()


async def test_console_info(api_client) -> None:
    assert await api_client.async_get_console_info() == ConsoleInfo(
        mac="AABBCCDDEEFF",
        name="Test Console",
        model="UDM-PRO",
        protect_version="7.2.105",
        firmware_version="5.1.33",
        external_alarm_manager=True,
    )


async def test_console_info_global_mode_off(api_client, fake_console) -> None:
    fake_console.nvr["featureFlags"] = {"useExternalAlarmManager": False}
    info = await api_client.async_get_console_info()
    assert info.external_alarm_manager is False


async def test_console_info_without_mac_is_unexpected(api_client, fake_console) -> None:
    del fake_console.nvr["mac"]
    with pytest.raises(UnexpectedResponse):
        await api_client.async_get_console_info()


async def test_wrong_password_is_auth_failed(api_client, fake_console) -> None:
    """The real console answers a wrong password with 403, not 401."""
    fake_console.password = "something-else"
    with pytest.raises(AuthFailed):
        await api_client.async_login()


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, AuthFailed),
        (401, AuthFailed),
        (403, AuthFailed),
        (429, RateLimited),
        (499, MfaRequired),
        (500, UnexpectedResponse),
    ],
)
async def test_login_status_mapping(api_client, fake_console, status, error) -> None:
    fake_console.login_status = status
    with pytest.raises(error):
        await api_client.async_get_profiles()


async def test_expired_session_logs_in_again_once(api_client, fake_console) -> None:
    await api_client.async_get_profiles()
    fake_console.expire_sessions()

    await api_client.async_get_profiles()

    assert fake_console.login_count == 2


async def test_concurrent_expiry_logs_in_once(api_client, fake_console) -> None:
    await api_client.async_get_profiles()
    fake_console.expire_sessions()

    await asyncio.gather(*(api_client.async_get_profiles() for _ in range(3)))

    assert fake_console.login_count == 2


async def test_not_super_admin_after_fresh_login(api_client, fake_console) -> None:
    fake_console.super_admin = False
    with pytest.raises(InsufficientPermissions):
        await api_client.async_get_profiles()
    # A 403 right after a fresh login is trusted immediately (spec section 3):
    # re-login-and-retry is only for a 403 on an already-established session.
    assert fake_console.login_count == 1


async def test_not_super_admin_on_established_session_logs_in_once_more(
    api_client, fake_console
) -> None:
    await api_client.async_get_profiles()
    fake_console.super_admin = False

    with pytest.raises(InsufficientPermissions):
        await api_client.async_get_profiles()

    # A 403 on an established session may be a stale CSRF token, so it still
    # gets one re-login-and-retry before being trusted.
    assert fake_console.login_count == 2


async def test_concurrent_fresh_requests_log_in_once(api_client, fake_console) -> None:
    await asyncio.gather(*(api_client.async_get_profiles() for _ in range(3)))

    assert fake_console.login_count == 1


async def test_arm_and_disarm_return_the_updated_profile(api_client) -> None:
    assert (await api_client.async_arm(AWAY_ID)).state == "arming"
    assert (await api_client.async_disarm(AWAY_ID)).state == "disarmed"


async def test_arm_unknown_profile_is_unexpected(api_client) -> None:
    with pytest.raises(UnexpectedResponse):
        await api_client.async_arm("00000000-0000-4000-8000-0000000000ff")


async def test_stale_csrf_on_post_logs_in_again(api_client, fake_console) -> None:
    await api_client.async_get_profiles()
    fake_console.rotate_csrf()

    assert (await api_client.async_arm(AWAY_ID)).state == "arming"
    assert fake_console.login_count == 2


async def test_updated_csrf_header_is_adopted(api_client, fake_console) -> None:
    fake_console.updated_csrf_on_get = "csrf-updated"
    await api_client.async_get_profiles()
    assert api_client.csrf_token == "csrf-updated"

    await api_client.async_arm(AWAY_ID)

    assert fake_console.login_count == 1


async def test_csrf_falls_back_to_the_jwt_claim(api_client, fake_console) -> None:
    fake_console.send_csrf_header = False
    fake_console.jwt_tokens = True

    await api_client.async_arm(AWAY_ID)

    assert api_client.csrf_token == "csrf-1"


async def test_login_without_any_csrf_is_unexpected(api_client, fake_console) -> None:
    fake_console.send_csrf_header = False
    with pytest.raises(UnexpectedResponse):
        await api_client.async_login()


@pytest.mark.parametrize(
    "payload",
    [
        {"not": "a list"},
        [{"id": AWAY_ID}],
        [{**profile_json(), "state_set_at": "yesterday"}],
        [{**profile_json(), "activation_delay": "60"}],
        [{**profile_json(), "state": None}],
    ],
)
async def test_schema_drift_is_unexpected(api_client, fake_console, payload) -> None:
    fake_console.profiles_payload = payload
    with pytest.raises(UnexpectedResponse) as err:
        await api_client.async_get_profiles()
    assert err.value.payload is not None


async def test_cannot_connect(http_session, socket_enabled) -> None:
    client = UniFiAlarmClient(
        http_session, "127.0.0.1:9", USERNAME, PASSWORD, scheme="http"
    )
    with pytest.raises(CannotConnect):
        await client.async_login()


async def test_ws_url_follows_scheme(http_session) -> None:
    secure = UniFiAlarmClient(http_session, "192.0.2.1", USERNAME, PASSWORD)
    plain = UniFiAlarmClient(
        http_session, "127.0.0.1:1", USERNAME, PASSWORD, scheme="http"
    )
    assert secure.ws_url() == "wss://192.0.2.1/proxy/protect/ws/updates"
    assert plain.ws_url() == "ws://127.0.0.1:1/proxy/protect/ws/updates"


async def test_fake_console_is_isolated_per_test(fake_console: FakeConsole) -> None:
    assert fake_console.login_count == 0


async def test_console_info_without_the_global_mode_flag_is_unknown(
    api_client, fake_console
) -> None:
    """A missing flag must not be read as "Global mode off" (firmware drift)."""
    del fake_console.nvr["featureFlags"]
    info = await api_client.async_get_console_info()
    assert info.external_alarm_manager is None


def test_timestamp_without_utc_offset_is_unexpected() -> None:
    data = {**profile_json(), "state_set_at": "2026-09-27T13:55:44"}
    with pytest.raises(UnexpectedResponse):
        ArmProfile.from_api(data)


async def test_persistent_403_is_not_retried_on_every_request(
    api_client, fake_console
) -> None:
    """An account demoted after setup must not cost a login on every poll."""
    await api_client.async_get_profiles()
    fake_console.super_admin = False

    for _ in range(3):
        with pytest.raises(InsufficientPermissions):
            await api_client.async_get_profiles()

    assert fake_console.login_count == 2  # one re-login, then trusted
