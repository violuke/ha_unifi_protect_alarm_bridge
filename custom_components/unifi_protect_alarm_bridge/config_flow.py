"""Config flow for UniFi Protect Alarm Bridge."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
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
from .const import (
    CONF_PROFILE_AWAY,
    CONF_PROFILE_HOME,
    CONF_PROFILE_NIGHT,
    DOMAIN,
    LOGGER,
    PROFILE_OPTION_KEYS,
)

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
        vol.Required(CONF_VERIFY_SSL, default=False): bool,
    }
)
STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
    }
)
STEP_RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_VERIFY_SSL): bool,
    }
)


def normalize_host(raw: str) -> str:
    """Reduce user input such as 'https://1.2.3.4/x' to a bare host[:port]."""
    value = raw.strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0]
    if value.count(":") > 1 and not value.startswith("["):
        value = f"[{value}]"  # bare IPv6 literal
    return value


def profiles_schema(
    profiles: Sequence[ArmProfile], current: Mapping[str, Any]
) -> vol.Schema:
    """Schema mapping HA arm modes to Protect profiles."""
    options = [
        SelectOptionDict(value=profile.id, label=profile.title)
        for profile in sorted(profiles, key=lambda profile: profile.title.casefold())
    ]
    known = {profile.id for profile in profiles}
    selector = SelectSelector(
        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    )

    def suggested(key: str) -> dict[str, Any]:
        value = current.get(key)
        return {"suggested_value": value} if value in known else {}

    return vol.Schema(
        {
            vol.Required(
                CONF_PROFILE_AWAY, description=suggested(CONF_PROFILE_AWAY)
            ): selector,
            vol.Optional(
                CONF_PROFILE_HOME, description=suggested(CONF_PROFILE_HOME)
            ): selector,
            vol.Optional(
                CONF_PROFILE_NIGHT, description=suggested(CONF_PROFILE_NIGHT)
            ): selector,
        }
    )


def mapping_from_input(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Keep only the arm modes the user mapped."""
    return {key: user_input[key] for key in PROFILE_OPTION_KEYS if user_input.get(key)}


def validate_mapping(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Return form errors; each profile may back only one arm mode."""
    chosen = list(mapping_from_input(user_input).values())
    if len(chosen) != len(set(chosen)):
        return {"base": "duplicate_profile"}
    return {}


class CannotValidate(Exception):
    """Validation failed; `error` is a config.error translation key."""

    def __init__(self, error: str) -> None:
        """Store the translation key."""
        super().__init__(error)
        self.error = error


async def async_validate_connection(
    hass: HomeAssistant, host: str, username: str, password: str, verify_ssl: bool
) -> tuple[ConsoleInfo, list[ArmProfile]]:
    """Log in and check Global mode and Super Admin access, or raise CannotValidate."""
    session = async_create_clientsession(
        hass,
        verify_ssl=verify_ssl,
        auto_cleanup=False,
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    )
    client = UniFiAlarmClient(session, host, username, password)
    try:
        console = await client.async_get_console_info()
        if not console.external_alarm_manager:
            raise CannotValidate("global_mode_off")
        profiles = await client.async_get_profiles()
    except AuthFailed as err:
        raise CannotValidate("invalid_auth") from err
    except MfaRequired as err:
        raise CannotValidate("mfa_not_supported") from err
    except RateLimited as err:
        raise CannotValidate("rate_limited") from err
    except CannotConnect as err:
        raise CannotValidate("cannot_connect") from err
    except InsufficientPermissions as err:
        raise CannotValidate("not_super_admin") from err
    except UnexpectedResponse as err:
        LOGGER.warning("Unexpected response while validating %s: %s", host, err)
        raise CannotValidate("unknown") from err
    except CannotValidate:
        raise
    except Exception as err:
        LOGGER.exception("Unexpected error while validating %s", host)
        raise CannotValidate("unknown") from err
    finally:
        # HA replaces close() on its sessions with a warning; detach() is the
        # documented way to release a session created with auto_cleanup=False.
        session.detach()
    if not profiles:
        raise CannotValidate("no_profiles")
    return console, profiles


class UniFiAlarmConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for UniFi Protect Alarm Bridge."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        self._data: dict[str, Any] = {}
        self._title = ""
        self._profiles: list[ArmProfile] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the console and a Super Admin account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**user_input, CONF_HOST: normalize_host(user_input[CONF_HOST])}
            try:
                console, profiles = await async_validate_connection(
                    self.hass,
                    data[CONF_HOST],
                    data[CONF_USERNAME],
                    data[CONF_PASSWORD],
                    data[CONF_VERIFY_SSL],
                )
            except CannotValidate as err:
                errors["base"] = err.error
            else:
                await self.async_set_unique_id(format_mac(console.mac))
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: data[CONF_HOST]}
                )
                self._data = data
                self._title = console.name
                self._profiles = profiles
                if len(profiles) == 1:
                    return self._async_create({CONF_PROFILE_AWAY: profiles[0].id})
                return await self.async_step_profiles()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA,
                {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD},
            ),
            errors=errors,
        )

    async def async_step_profiles(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Map HA arm modes to Protect profiles."""
        errors: dict[str, str] = {}
        if user_input is not None and not (errors := validate_mapping(user_input)):
            return self._async_create(mapping_from_input(user_input))
        return self.async_show_form(
            step_id="profiles",
            data_schema=profiles_schema(self._profiles, user_input or {}),
            errors=errors,
        )

    def _async_create(self, options: dict[str, str]) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._title, data=self._data, options=options
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after the console rejected the credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new credentials for the same console."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                console, _ = await async_validate_connection(
                    self.hass,
                    entry.data[CONF_HOST],
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    entry.data[CONF_VERIFY_SSL],
                )
            except CannotValidate as err:
                errors["base"] = err.error
            else:
                await self.async_set_unique_id(format_mac(console.mac))
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_REAUTH_SCHEMA, {CONF_USERNAME: entry.data[CONF_USERNAME]}
            ),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the host or SSL verification of the same console."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            updates = {
                CONF_HOST: normalize_host(user_input[CONF_HOST]),
                CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
            }
            try:
                console, _ = await async_validate_connection(
                    self.hass,
                    updates[CONF_HOST],
                    entry.data[CONF_USERNAME],
                    entry.data[CONF_PASSWORD],
                    updates[CONF_VERIFY_SSL],
                )
            except CannotValidate as err:
                errors["base"] = err.error
            else:
                await self.async_set_unique_id(format_mac(console.mac))
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, data_updates=updates)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_RECONFIGURE_SCHEMA, user_input or entry.data
            ),
            errors=errors,
        )
