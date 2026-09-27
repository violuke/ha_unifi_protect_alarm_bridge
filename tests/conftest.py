"""Shared fixtures."""

from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_protect_alarm_bridge.api import UniFiAlarmClient

from .fake_console import FakeConsole
from .helpers import CONSOLE, PASSWORD, USERNAME, make_profile, titled_profile


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load integrations from custom_components/."""


@pytest.fixture
async def fake_console(socket_enabled) -> AsyncGenerator[FakeConsole]:
    """A running fake console on 127.0.0.1 (sockets are otherwise blocked)."""
    console = FakeConsole()
    await console.start()
    yield console
    await console.stop()


@pytest.fixture
async def http_session() -> AsyncGenerator[aiohttp.ClientSession]:
    """A session configured exactly like the integration's (unsafe cookie jar)."""
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    yield session
    await session.close()


@pytest.fixture
def api_client(
    fake_console: FakeConsole, http_session: aiohttp.ClientSession
) -> UniFiAlarmClient:
    """A real client pointed at the fake console over plain HTTP."""
    return UniFiAlarmClient(
        http_session, fake_console.host, USERNAME, PASSWORD, scheme="http"
    )


@pytest.fixture
def mock_listener_run() -> Generator[AsyncMock]:
    """Don't open a real websocket during integration tests."""
    with patch(
        "custom_components.unifi_protect_alarm_bridge.websocket.ProtectUpdatesListener.run",
        new_callable=AsyncMock,
    ) as run:
        yield run


@pytest.fixture
def mock_client(mock_listener_run: AsyncMock) -> Generator[MagicMock]:
    """Replace the API client used by async_setup_entry."""
    with patch(
        "custom_components.unifi_protect_alarm_bridge.UniFiAlarmClient", autospec=True
    ) as client_cls:
        client = client_cls.return_value
        client.async_get_console_info.return_value = CONSOLE
        client.async_get_profiles.return_value = [make_profile()]
        client.async_arm.side_effect = lambda pid: titled_profile(pid, "arming")
        client.async_disarm.side_effect = lambda pid: titled_profile(pid, "disarmed")
        yield client


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up an entry, and wait for it to settle."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
