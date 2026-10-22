"""Use the official Protect integration's session without copying credentials."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util.ssl import get_default_context

from .api import (
    NVR_PATH,
    PROFILES_PATH,
    REQUEST_TIMEOUT,
    ArmProfile,
    CannotConnect,
    ConsoleInfo,
    InsufficientPermissions,
    RateLimited,
    UnexpectedResponse,
)


class SharedProtectClient:
    """The official integration owns authentication and session lifetime."""

    def __init__(
        self,
        hass: HomeAssistant,
        source_entry_id: str,
        certificate_fingerprint: str | None = None,
    ) -> None:
        self.hass = hass
        self.source_entry_id = source_entry_id
        self.ssl = (
            aiohttp.Fingerprint(bytes.fromhex(certificate_fingerprint))
            if certificate_fingerprint
            else get_default_context()
        )
        self._profile_ids: set[str] = set()

    def _source_api(self):
        entry = self.hass.config_entries.async_get_entry(self.source_entry_id)
        if (
            entry is None
            or entry.domain != "unifiprotect"
            or entry.state is not ConfigEntryState.LOADED
            or not hasattr(entry, "runtime_data")
        ):
            raise CannotConnect("The existing Protect integration is not loaded")
        api = getattr(entry.runtime_data, "api", None)
        if api is None:
            raise CannotConnect("The existing Protect runtime is not available")
        # Reject API-key-only connections when exposed by the source client.
        if getattr(api, "is_public_only", False) or not api.is_authenticated():
            raise CannotConnect("The existing Protect session is not authenticated")
        return api

    async def _request(self, method: str, path: str):
        permitted_read = method == "GET" and path in (NVR_PATH, PROFILES_PATH)
        match = re.fullmatch(
            re.escape(PROFILES_PATH) + r"/([A-Za-z0-9_-]+)/actions/(arm|disarm)", path
        )
        permitted_write = method == "POST" and match and match[1] in self._profile_ids
        if not (permitted_read or permitted_write):
            raise UnexpectedResponse("Unsupported alarm operation")
        api = self._source_api()
        origin = urlsplit(api.base_url)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
        ):
            raise CannotConnect("The existing console URL is not a secure origin")
        try:
            session = await api.get_session()
            # Read current headers on each request; never read entry.data,
            # trigger login/logout, change TLS defaults, or close this session.
            async with session.request(
                method,
                f"https://{origin.netloc}{path}",
                headers=api.headers,
                ssl=self.ssl,
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status == 401:
                    raise CannotConnect("The existing Protect session needs to recover")
                if response.status == 403:
                    raise InsufficientPermissions(
                        "Protect rejected the alarm operation"
                    )
                if response.status == 429:
                    raise RateLimited("Protect is limiting alarm requests")
                if response.status != 200:
                    raise UnexpectedResponse("Unexpected alarm response status")
                try:
                    return await response.json()
                except ValueError as err:
                    raise UnexpectedResponse(
                        "Alarm response is not valid JSON"
                    ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            # A failed POST has an uncertain outcome. Never retry it blindly.
            raise CannotConnect("Alarm connection or certificate check failed") from err

    async def async_get_console_info(self) -> ConsoleInfo:
        return ConsoleInfo.from_api(await self._request("GET", NVR_PATH))

    async def async_get_profiles(self) -> list[ArmProfile]:
        payload = await self._request("GET", PROFILES_PATH)
        if not isinstance(payload, list):
            raise UnexpectedResponse("Profiles response is not a list")
        profiles = [ArmProfile.from_api(profile) for profile in payload]
        self._profile_ids = {profile.id for profile in profiles}
        return profiles

    async def async_arm(self, profile_id: str) -> ArmProfile:
        return ArmProfile.from_api(
            await self._request("POST", f"{PROFILES_PATH}/{profile_id}/actions/arm")
        )

    async def async_disarm(self, profile_id: str) -> ArmProfile:
        return ArmProfile.from_api(
            await self._request("POST", f"{PROFILES_PATH}/{profile_id}/actions/disarm")
        )
