"""Shared fixtures."""

from collections.abc import AsyncGenerator

import aiohttp
import pytest

from custom_components.unifi_protect_alarm_bridge.api import UniFiAlarmClient

from .fake_console import FakeConsole
from .helpers import PASSWORD, USERNAME


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
