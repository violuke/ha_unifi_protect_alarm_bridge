"""Listener for Protect's realtime update websocket (no Home Assistant imports)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import json
import logging
import struct
from typing import Any
import zlib

import aiohttp

from .api import (
    ArmProfile,
    AuthFailed,
    CannotConnect,
    MfaRequired,
    RateLimited,
    UnexpectedResponse,
    UniFiAlarmClient,
)

_LOGGER = logging.getLogger(__name__)

# Each frame: packet type, payload format, deflated flag, reserved, payload size.
_HEADER = struct.Struct(">BBBBI")
_FORMAT_JSON = 1
_FORMAT_UTF8 = 2
PROFILE_MODEL_KEY = "externalArmProfile"
_CLOSED_TYPES = (
    aiohttp.WSMsgType.CLOSE,
    aiohttp.WSMsgType.CLOSING,
    aiohttp.WSMsgType.CLOSED,
    aiohttp.WSMsgType.ERROR,
)


class PacketDecodeError(ValueError):
    """A websocket packet could not be decoded."""


def decode_packet(data: bytes) -> list[Any]:
    """Split a Protect update packet into its decoded frames."""
    frames: list[Any] = []
    offset = 0
    while offset < len(data):
        if offset + _HEADER.size > len(data):
            raise PacketDecodeError("truncated frame header")
        _type, payload_format, deflated, _reserved, size = _HEADER.unpack_from(
            data, offset
        )
        offset += _HEADER.size
        payload = data[offset : offset + size]
        if len(payload) != size:
            raise PacketDecodeError("truncated frame payload")
        offset += size
        try:
            if deflated:
                payload = zlib.decompress(payload)
            if payload_format == _FORMAT_JSON:
                frames.append(json.loads(payload))
            elif payload_format == _FORMAT_UTF8:
                frames.append(payload.decode())
            else:
                frames.append(payload)
        except (zlib.error, ValueError) as err:
            raise PacketDecodeError(f"undecodable frame: {err}") from err
    return frames


class ProtectUpdatesListener:
    """Keep a websocket open and forward arm profile updates."""

    def __init__(
        self,
        client: UniFiAlarmClient,
        *,
        on_profile: Callable[[ArmProfile], None],
        on_connection_change: Callable[[bool], None],
        on_resync: Callable[[], None],
        on_auth_failed: Callable[[], None],
        heartbeat: float = 30.0,
        silence_timeout: float = 120.0,
        backoff_initial: float = 1.0,
        backoff_max: float = 300.0,
    ) -> None:
        """Store callbacks; call run() to start."""
        self._client = client
        self._on_profile = on_profile
        self._on_connection_change = on_connection_change
        self._on_resync = on_resync
        self._on_auth_failed = on_auth_failed
        self._heartbeat = heartbeat
        self._silence_timeout = silence_timeout
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._warned = False
        self._silence_drops = 0
        self.connected = False
        self.last_message_at: datetime | None = None
        self.reconnect_count = 0

    async def run(self) -> None:
        """Connect and listen forever, until cancelled or the credentials fail."""
        backoff = self._backoff_initial
        while True:
            established = False
            try:
                established = await self._connect_and_listen()
            except AuthFailed, MfaRequired:
                _LOGGER.error(
                    "Protect update socket stopped: the console rejected the "
                    "stored credentials"
                )
                self._set_connected(False)
                self._on_auth_failed()
                return
            except (
                CannotConnect,
                RateLimited,
                UnexpectedResponse,
                aiohttp.ClientError,
                TimeoutError,
            ) as err:
                self._log_drop(f"{type(err).__name__}: {err}")
            except Exception:
                # A bug (ours or a callback's) must not end instant updates for good.
                _LOGGER.exception("Unexpected error in the Protect update socket")
            self._set_connected(False)
            if established:
                backoff = self._backoff_initial
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._backoff_max)
            self.reconnect_count += 1

    async def _connect_and_listen(self) -> bool:
        ws = await self._connect()
        try:
            self._set_connected(True)
            while True:
                try:
                    message = await asyncio.wait_for(
                        ws.receive(), self._silence_timeout
                    )
                except TimeoutError:
                    reason = f"no messages for {self._silence_timeout:.0f}s"
                    if self._silence_drops:
                        # Quiet sites hit this regularly; only the first is a warning.
                        _LOGGER.debug("Protect update socket silent (%s)", reason)
                    else:
                        self._log_drop(reason)
                    self._silence_drops += 1
                    return True
                if message.type is aiohttp.WSMsgType.BINARY:
                    self.last_message_at = datetime.now(UTC)
                    self._handle_binary(message.data)
                elif message.type in _CLOSED_TYPES:
                    self._log_drop(f"socket closed ({message.type.name})")
                    return True
        finally:
            await self._close(ws)

    async def _close(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await ws.close()
        # Work around an aiohttp quirk: when *we* initiate the close, the
        # peer's close-handshake byte can arrive a tick after ws.close()
        # already cancelled the heartbeat, and aiohttp's _on_data_received()
        # re-arms it without checking that the socket is closed. Left alone,
        # that phantom `_send_heartbeat` timer keeps the closed connection
        # alive until it eventually fires. Yield once so any pending reset
        # lands, then cancel it.
        await asyncio.sleep(0)
        heartbeat_cb = getattr(ws, "_heartbeat_cb", None)
        if heartbeat_cb is not None:
            heartbeat_cb.cancel()

    async def _connect(self) -> aiohttp.ClientWebSocketResponse:
        if self._client.csrf_token is None:
            await self._client.async_login()
        generation = self._client.generation
        try:
            return await self._open()
        except aiohttp.WSServerHandshakeError as err:
            if err.status not in (401, 403):
                raise CannotConnect(f"handshake failed with HTTP {err.status}") from err
        # Raises AuthFailed if the password has changed; run() stops on that.
        await self._client.async_login(stale_generation=generation)
        try:
            return await self._open()
        except aiohttp.WSServerHandshakeError as err:
            raise CannotConnect(
                f"handshake failed with HTTP {err.status} after a fresh login"
            ) from err

    async def _open(self) -> aiohttp.ClientWebSocketResponse:
        return await self._client.session.ws_connect(
            self._client.ws_url(),
            headers={"X-CSRF-Token": self._client.csrf_token or ""},
            heartbeat=self._heartbeat,
        )

    def _handle_binary(self, data: bytes) -> None:
        try:
            frames = decode_packet(data)
        except PacketDecodeError as err:
            _LOGGER.debug("Ignoring undecodable update packet: %s", err)
            return
        if len(frames) < 2 or not isinstance(frames[0], dict):
            return
        action = frames[0]
        if (
            action.get("modelKey") != PROFILE_MODEL_KEY
            or action.get("action") != "update"
        ):
            return
        try:
            profile = ArmProfile.from_api(frames[1])
        except UnexpectedResponse:
            _LOGGER.debug("Arm profile update was not a full profile; resyncing")
            self._on_resync()
            return
        self._on_profile(profile)

    def _set_connected(self, connected: bool) -> None:
        if connected == self.connected:
            return
        self.connected = connected
        if connected:
            if self._warned:
                _LOGGER.info("Protect update socket reconnected")
            self._warned = False
        self._on_connection_change(connected)

    def _log_drop(self, reason: str) -> None:
        if self._warned:
            _LOGGER.debug("Protect update socket still down: %s", reason)
            return
        self._warned = True
        _LOGGER.warning(
            "Protect update socket disconnected (%s); falling back to polling", reason
        )
