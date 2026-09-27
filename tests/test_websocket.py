"""Tests for the Protect update websocket listener."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
import contextlib
import struct
from typing import Any

import pytest

from custom_components.unifi_protect_alarm_bridge.api import (
    ArmProfile,
    UniFiAlarmClient,
)
from custom_components.unifi_protect_alarm_bridge.websocket import (
    PacketDecodeError,
    ProtectUpdatesListener,
    decode_packet,
)

from .helpers import (
    AWAY_ID,
    PASSWORD,
    USERNAME,
    encode_packet,
    profile_json,
    profile_packet,
)


@pytest.mark.parametrize("deflate", [False, True])
def test_decode_json_frames(deflate: bool) -> None:
    packet = encode_packet({"action": "update"}, {"a": 1}, deflate=deflate)
    assert decode_packet(packet) == [{"action": "update"}, {"a": 1}]


def test_decode_utf8_frame() -> None:
    payload = b"hello"
    packet = struct.pack(">BBBBI", 1, 2, 0, 0, len(payload)) + payload
    assert decode_packet(packet) == ["hello"]


@pytest.mark.parametrize(
    "packet",
    [
        b"\x01\x01\x00",
        encode_packet({"a": 1}, {"b": 2})[:-3],
        struct.pack(">BBBBI", 1, 1, 1, 0, 3) + b"xyz",  # "deflated" but not zlib
    ],
)
def test_decode_rejects_bad_packets(packet: bytes) -> None:
    with pytest.raises(PacketDecodeError):
        decode_packet(packet)


class Recorder:
    """Collects listener callbacks."""

    def __init__(self) -> None:
        self.profiles: list[ArmProfile] = []
        self.connection: list[bool] = []
        self.resyncs = 0
        self.auth_failed = 0

    def on_profile(self, profile: ArmProfile) -> None:
        self.profiles.append(profile)

    def on_connection_change(self, connected: bool) -> None:
        self.connection.append(connected)

    def on_resync(self) -> None:
        self.resyncs += 1

    def on_auth_failed(self) -> None:
        self.auth_failed += 1


async def wait_until(predicate: Callable[[], bool], limit: float = 5) -> None:
    async with asyncio.timeout(limit):
        while not predicate():  # noqa: ASYNC110 - polling test callbacks is intended
            await asyncio.sleep(0.01)


@pytest.fixture
async def start_listener() -> AsyncGenerator[
    Callable[..., tuple[ProtectUpdatesListener, Recorder, asyncio.Task[None]]]
]:
    tasks: list[asyncio.Task[None]] = []

    def _start(
        client: UniFiAlarmClient, **kwargs: Any
    ) -> tuple[ProtectUpdatesListener, Recorder, asyncio.Task[None]]:
        recorder = Recorder()
        options = {"backoff_initial": 0.01, "backoff_max": 0.05, **kwargs}
        listener = ProtectUpdatesListener(
            client,
            on_profile=recorder.on_profile,
            on_connection_change=recorder.on_connection_change,
            on_resync=recorder.on_resync,
            on_auth_failed=recorder.on_auth_failed,
            **options,
        )
        task = asyncio.create_task(listener.run())
        tasks.append(task)
        return listener, recorder, task

    yield _start
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("deflate", [False, True])
async def test_forwards_profile_updates(
    api_client, fake_console, start_listener, deflate
) -> None:
    listener, recorder, _ = start_listener(api_client)
    await wait_until(lambda: recorder.connection == [True])

    await fake_console.ws_queue.put(
        profile_packet(profile_json(state="arming"), deflate=deflate)
    )
    await wait_until(lambda: len(recorder.profiles) == 1)

    assert recorder.profiles[0].id == AWAY_ID
    assert recorder.profiles[0].state == "arming"
    assert listener.connected is True
    assert listener.last_message_at is not None


async def test_ignores_other_models_and_garbage(
    api_client, fake_console, start_listener
) -> None:
    _, recorder, _ = start_listener(api_client)
    await wait_until(lambda: recorder.connection == [True])

    await fake_console.ws_queue.put(
        encode_packet(
            {"action": "update", "modelKey": "sensor", "id": "x"},
            {"isMotionDetected": True},
        )
    )
    await fake_console.ws_queue.put(b"\x00\x01")
    await fake_console.ws_queue.put(profile_packet(profile_json(state="armed")))
    await wait_until(lambda: len(recorder.profiles) == 1)

    assert recorder.profiles[0].state == "armed"
    assert recorder.resyncs == 0


async def test_partial_profile_update_requests_resync(
    api_client, fake_console, start_listener
) -> None:
    _, recorder, _ = start_listener(api_client)
    await wait_until(lambda: recorder.connection == [True])

    await fake_console.ws_queue.put(profile_packet({"id": AWAY_ID, "state": "armed"}))
    await wait_until(lambda: recorder.resyncs == 1)

    assert recorder.profiles == []


async def test_reconnects_after_server_closes(
    api_client, fake_console, start_listener
) -> None:
    listener, recorder, _ = start_listener(api_client)
    await wait_until(lambda: recorder.connection == [True])

    await fake_console.ws_queue.put(None)  # server closes the socket
    await wait_until(lambda: recorder.connection == [True, False, True])

    assert fake_console.ws_connections == 2
    assert listener.reconnect_count >= 1


async def test_silent_socket_is_treated_as_dead(
    api_client, fake_console, start_listener, caplog
) -> None:
    _, recorder, _ = start_listener(api_client, silence_timeout=0.2)
    await wait_until(lambda: recorder.connection.count(True) >= 3)

    assert fake_console.ws_connections >= 3
    warnings = [r for r in caplog.records if "disconnected" in r.getMessage()]
    assert len(warnings) == 1  # repeat silence drops are logged at debug only


async def test_mfa_challenge_stops_the_listener(
    api_client, fake_console, start_listener
) -> None:
    await api_client.async_login()
    fake_console.expire_sessions()
    fake_console.login_status = 499

    _, recorder, task = start_listener(api_client)
    await wait_until(task.done)

    assert recorder.auth_failed == 1


async def test_rate_limited_login_backs_off_and_retries(
    api_client, fake_console, start_listener
) -> None:
    fake_console.login_status = 429
    listener, recorder, task = start_listener(api_client)

    await wait_until(lambda: listener.reconnect_count >= 2)
    assert not task.done()
    assert recorder.auth_failed == 0

    fake_console.login_status = None
    await wait_until(lambda: recorder.connection == [True])


async def test_callback_error_does_not_kill_the_listener(
    api_client, fake_console, start_listener
) -> None:
    listener, recorder, task = start_listener(api_client)
    await wait_until(lambda: recorder.connection == [True])
    calls = 0

    def exploding_on_profile(profile: ArmProfile) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("bug in a callback")

    listener._on_profile = exploding_on_profile
    await fake_console.ws_queue.put(profile_packet(profile_json(state="armed")))

    await wait_until(lambda: recorder.connection == [True, False, True])
    assert calls == 1
    assert not task.done()


async def test_logs_in_again_when_handshake_is_unauthorised(
    api_client, fake_console, start_listener
) -> None:
    await api_client.async_login()
    fake_console.expire_sessions()

    _, recorder, _ = start_listener(api_client)
    await wait_until(lambda: recorder.connection == [True])

    assert fake_console.login_count == 2


async def test_auth_failure_stops_the_listener(
    api_client, fake_console, start_listener
) -> None:
    await api_client.async_login()
    fake_console.expire_sessions()
    fake_console.password = "changed-by-the-user"

    _, recorder, task = start_listener(api_client)
    await wait_until(task.done)

    assert recorder.auth_failed == 1
    assert recorder.connection == []
    assert fake_console.login_count == 2  # exactly one failed retry, no loop


async def test_keeps_retrying_when_console_is_unreachable(
    http_session, socket_enabled, start_listener
) -> None:
    client = UniFiAlarmClient(
        http_session, "127.0.0.1:9", USERNAME, PASSWORD, scheme="http"
    )
    listener, recorder, task = start_listener(client)

    await wait_until(lambda: listener.reconnect_count >= 2)

    assert not task.done()
    assert recorder.connection == []
