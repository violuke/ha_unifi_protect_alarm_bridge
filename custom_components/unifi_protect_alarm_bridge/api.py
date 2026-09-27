"""Client for the UniFi OS Alarm Manager API.

Pure aiohttp with no Home Assistant imports. Every undocumented detail of the
console's API lives in this module (see the spec, section 3).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any

import aiohttp

LOGIN_PATH = "/api/auth/login"
NVR_PATH = "/proxy/protect/api/nvr"
PROFILES_PATH = "/api/v2/alarms/profiles"
UPDATES_WS_PATH = "/proxy/protect/ws/updates"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class UniFiAlarmError(Exception):
    """Base error for the UniFi alarm client."""


class AuthFailed(UniFiAlarmError):
    """The console rejected the username or password."""


class MfaRequired(UniFiAlarmError):
    """The account has two-factor authentication enabled."""


class RateLimited(UniFiAlarmError):
    """The console is refusing logins after too many failures."""


class InsufficientPermissions(UniFiAlarmError):
    """The account is logged in but may not use Alarm Manager (not Super Admin)."""


class CannotConnect(UniFiAlarmError):
    """Network, TLS or timeout failure."""


class UnexpectedResponse(UniFiAlarmError):
    """The console answered with something this client does not understand."""

    def __init__(self, message: str, payload: Any = None) -> None:
        """Keep the raw payload for diagnostics."""
        super().__init__(message)
        self.payload = payload


def _parse_timestamp(value: Any, payload: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise UnexpectedResponse("timestamp is not a string", payload)
    try:
        return datetime.fromisoformat(value)
    except ValueError as err:
        raise UnexpectedResponse(f"invalid timestamp {value!r}", payload) from err


@dataclass(frozen=True, slots=True)
class ArmProfile:
    """One Alarm Manager arm profile and its current state."""

    id: str
    title: str
    state: str
    activation_delay: int | None
    state_set_at: datetime | None
    state_promotion_due_at: datetime | None

    @classmethod
    def from_api(cls, data: Any) -> ArmProfile:
        """Parse a profile from REST or websocket JSON."""
        if not isinstance(data, dict):
            raise UnexpectedResponse("profile is not an object", data)
        profile_id, title, state = data.get("id"), data.get("title"), data.get("state")
        if not all(isinstance(value, str) for value in (profile_id, title, state)):
            raise UnexpectedResponse("profile is missing id, title or state", data)
        delay = data.get("activation_delay")
        if delay is not None and (
            not isinstance(delay, int) or isinstance(delay, bool)
        ):
            raise UnexpectedResponse("activation_delay is not an integer", data)
        return cls(
            id=profile_id,
            title=title,
            state=state,
            activation_delay=delay,
            state_set_at=_parse_timestamp(data.get("state_set_at"), data),
            state_promotion_due_at=_parse_timestamp(
                data.get("state_promotion_due_at"), data
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict (for diagnostics)."""
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "activation_delay": self.activation_delay,
            "state_set_at": self.state_set_at.isoformat()
            if self.state_set_at
            else None,
            "state_promotion_due_at": (
                self.state_promotion_due_at.isoformat()
                if self.state_promotion_due_at
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class ConsoleInfo:
    """Identity of the console, from GET /proxy/protect/api/nvr."""

    mac: str
    name: str
    model: str | None
    protect_version: str | None
    firmware_version: str | None
    external_alarm_manager: bool

    @classmethod
    def from_api(cls, data: Any) -> ConsoleInfo:
        """Parse the Protect NVR object."""
        if not isinstance(data, dict):
            raise UnexpectedResponse("console info is not an object", data)
        mac, name = data.get("mac"), data.get("name")
        if not isinstance(mac, str) or not isinstance(name, str):
            raise UnexpectedResponse("console info is missing mac or name", data)
        flags = data.get("featureFlags")
        flags = flags if isinstance(flags, dict) else {}
        model, version = data.get("type"), data.get("version")
        firmware = data.get("firmwareVersion")
        return cls(
            mac=mac,
            name=name,
            model=model if isinstance(model, str) else None,
            protect_version=version if isinstance(version, str) else None,
            firmware_version=firmware if isinstance(firmware, str) else None,
            external_alarm_manager=flags.get("useExternalAlarmManager") is True,
        )


class UniFiAlarmClient:
    """Session-authenticated client for one UniFi console."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        *,
        scheme: str = "https",
    ) -> None:
        """Store connection details. `session` must use CookieJar(unsafe=True)."""
        self._session = session
        self._base_url = f"{scheme}://{host}"
        self._ws_base_url = f"{'wss' if scheme == 'https' else 'ws'}://{host}"
        self._username = username
        self._password = password
        self._csrf_token: str | None = None
        self._generation = 0
        self._login_lock = asyncio.Lock()

    @property
    def session(self) -> aiohttp.ClientSession:
        """The aiohttp session (shared with the websocket listener)."""
        return self._session

    @property
    def csrf_token(self) -> str | None:
        """The current CSRF token, if logged in."""
        return self._csrf_token

    @property
    def generation(self) -> int:
        """Incremented on every successful login."""
        return self._generation

    def ws_url(self, path: str = UPDATES_WS_PATH) -> str:
        """Return the websocket URL for `path`."""
        return self._ws_base_url + path

    async def async_login(self, *, stale_generation: int | None = None) -> None:
        """Log in, unless another caller already replaced `stale_generation`."""
        async with self._login_lock:
            if stale_generation is not None and stale_generation != self._generation:
                return
            try:
                async with self._session.post(
                    self._base_url + LOGIN_PATH,
                    json={
                        "username": self._username,
                        "password": self._password,
                        "rememberMe": True,
                    },
                    timeout=REQUEST_TIMEOUT,
                ) as resp:
                    status = resp.status
                    header_token = resp.headers.get(
                        "X-Updated-CSRF-Token"
                    ) or resp.headers.get("X-CSRF-Token")
                    await resp.read()
            except (aiohttp.ClientError, TimeoutError) as err:
                raise CannotConnect(f"login failed: {err}") from err
            if status == 499:
                raise MfaRequired("the account requires two-factor authentication")
            if status == 429:
                raise RateLimited("the console is rate limiting logins")
            if status in (400, 401, 403):
                raise AuthFailed(f"login rejected with HTTP {status}")
            if status != 200:
                raise UnexpectedResponse(f"login returned HTTP {status}")
            token = header_token or self._csrf_from_cookie()
            if not token:
                raise UnexpectedResponse("login response had no CSRF token")
            self._csrf_token = token
            self._generation += 1

    def _csrf_from_cookie(self) -> str | None:
        """Read the csrfToken claim from the TOKEN JWT (uiprotect does the same)."""
        for cookie in self._session.cookie_jar:
            if cookie.key != "TOKEN":
                continue
            parts = cookie.value.split(".")
            if len(parts) != 3:
                return None
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            try:
                claims = json.loads(base64.urlsafe_b64decode(padded))
            except binascii.Error, ValueError:
                return None
            token = claims.get("csrfToken") if isinstance(claims, dict) else None
            return token if isinstance(token, str) else None
        return None

    async def _send(self, method: str, path: str) -> tuple[int, int, Any]:
        generation = self._generation
        headers = {"Accept": "application/json"}
        if self._csrf_token:
            headers["X-CSRF-Token"] = self._csrf_token
        try:
            async with self._session.request(
                method, self._base_url + path, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                if updated := resp.headers.get("X-Updated-CSRF-Token"):
                    self._csrf_token = updated
                status = resp.status
                body = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(f"{method} {path} failed: {err}") from err
        payload: Any = None
        if body:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = body.decode(errors="replace")
        return status, generation, payload

    async def _request(self, method: str, path: str) -> Any:
        fresh = self._csrf_token is None
        if fresh:
            await self.async_login(stale_generation=self._generation)
        status, generation, payload = await self._send(method, path)
        if fresh and status == 403:
            # A 403 on GET /profiles right after a fresh login means the account
            # is not Super Admin, not a stale CSRF token: don't waste a login on it.
            raise InsufficientPermissions(
                f"{method} {path} is forbidden for this account"
            )
        if status in (401, 403):
            # 401: session expired. 403: possibly a stale CSRF token on an
            # established session. Either way, only trust the answer after one
            # fresh login.
            await self.async_login(stale_generation=generation)
            status, _, payload = await self._send(method, path)
        if status == 401:
            raise UnexpectedResponse(
                f"{method} {path} is still unauthorised after a fresh login", payload
            )
        if status == 403:
            raise InsufficientPermissions(
                f"{method} {path} is forbidden for this account"
            )
        if status == 429:
            raise RateLimited(f"{method} {path} was rate limited")
        if not 200 <= status < 300:
            raise UnexpectedResponse(f"{method} {path} returned HTTP {status}", payload)
        if isinstance(payload, str):
            raise UnexpectedResponse(f"{method} {path} returned non-JSON", payload)
        return payload

    async def async_get_console_info(self) -> ConsoleInfo:
        """Return the console identity and Global-mode flag."""
        return ConsoleInfo.from_api(await self._request("GET", NVR_PATH))

    async def async_get_profiles(self) -> list[ArmProfile]:
        """Return every Alarm Manager arm profile."""
        payload = await self._request("GET", PROFILES_PATH)
        if not isinstance(payload, list):
            raise UnexpectedResponse("profiles response is not a list", payload)
        return [ArmProfile.from_api(item) for item in payload]

    async def async_arm(self, profile_id: str) -> ArmProfile:
        """Arm a profile; returns the updated profile (usually `arming`)."""
        path = f"{PROFILES_PATH}/{profile_id}/actions/arm"
        return ArmProfile.from_api(await self._request("POST", path))

    async def async_disarm(self, profile_id: str) -> ArmProfile:
        """Disarm a profile; returns the updated profile."""
        path = f"{PROFILES_PATH}/{profile_id}/actions/disarm"
        return ArmProfile.from_api(await self._request("POST", path))
