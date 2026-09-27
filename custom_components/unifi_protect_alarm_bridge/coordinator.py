"""Coordinator merging websocket pushes, action responses and polls."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.redact import async_redact_data
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

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
    UniFiAlarmError,
)
from .const import (
    ALL_ISSUES,
    DOMAIN,
    ISSUE_API_CHANGED,
    ISSUE_GLOBAL_MODE_OFF,
    ISSUE_NOT_SUPER_ADMIN,
    ISSUE_PROFILE_MISSING,
    ISSUE_PUSH_UNAVAILABLE,
    ISSUE_TRACKER_URL,
    LOGGER,
    POLL_INTERVAL_PUSH_DOWN,
    POLL_INTERVAL_PUSH_HEALTHY,
    PROFILE_OPTION_KEYS,
    PROMOTION_GRACE_SECONDS,
    PROMOTION_MAX_OVERDUE,
    PROMOTION_MIN_DELAY_SECONDS,
    PUSH_UNAVAILABLE_AFTER,
    REDACT_KEYS,
    STATE_ARMING,
)
from .websocket import ProtectUpdatesListener

type UniFiAlarmConfigEntry = ConfigEntry[UniFiAlarmCoordinator]


@callback
def async_delete_entry_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Delete every repair issue this integration may have raised for an entry."""
    for key in ALL_ISSUES:
        ir.async_delete_issue(hass, DOMAIN, f"{key}_{entry_id}")


def _derive_missing_promotion_due_at(profile: ArmProfile) -> ArmProfile:
    """Fill in a missing promotion due time for an arming profile.

    Protect's websocket profile payloads omit `state_promotion_due_at`
    (verified live 2026-09-27), even though the REST arm response carries it.
    Derive it from `state_set_at + activation_delay` so the entity attribute
    and the promotion-refresh backstop both still see a due time.
    """
    if (
        profile.state == STATE_ARMING
        and profile.state_promotion_due_at is None
        and profile.state_set_at is not None
        and profile.activation_delay is not None
        and profile.activation_delay > 0
    ):
        return replace(
            profile,
            state_promotion_due_at=profile.state_set_at
            + timedelta(seconds=profile.activation_delay),
        )
    return profile


class UniFiAlarmCoordinator(DataUpdateCoordinator[dict[str, ArmProfile]]):
    """Holds every arm profile, keyed by profile ID."""

    config_entry: UniFiAlarmConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: UniFiAlarmConfigEntry,
        client: UniFiAlarmClient,
        console: ConsoleInfo,
    ) -> None:
        """Create the coordinator; call async_start_push() after the first refresh."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=POLL_INTERVAL_PUSH_DOWN,
            always_update=False,
        )
        self.client = client
        self.console = console
        self.listener = ProtectUpdatesListener(
            client,
            on_profile=self.handle_profile,
            on_connection_change=self.handle_connection_change,
            on_resync=self.handle_resync,
            on_auth_failed=self.handle_auth_failed,
        )
        self.last_poll_success_at: datetime | None = None
        self.last_unexpected_payload: Any = None
        self._push_down_since: datetime | None = dt_util.utcnow()
        # Stale-write guard: pushes and action responses stamp the profiles they
        # write, and a poll ignores any profile stamped after the poll started.
        self._write_seq = 0
        self._written_seq: dict[str, int] = {}
        self._promotion_unsub: CALLBACK_TYPE | None = None
        self._overdue_counts: dict[str, int] = {}
        self._unexpected_logged = False

    @callback
    def async_start_push(self) -> None:
        """Run the websocket listener until the entry unloads."""
        self.config_entry.async_create_background_task(
            self.hass,
            self.listener.run(),
            name=f"{DOMAIN} update socket {self.config_entry.entry_id}",
        )

    async def async_shutdown(self) -> None:
        """Cancel timers on unload."""
        self._cancel_promotion()
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, ArmProfile]:
        seq_at_start = self._write_seq
        try:
            profiles = await self.client.async_get_profiles()
        except (AuthFailed, MfaRequired) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except InsufficientPermissions as err:
            self._create_issue(ISSUE_NOT_SUPER_ADMIN)
            raise UpdateFailed(str(err)) from err
        except (CannotConnect, RateLimited) as err:
            raise UpdateFailed(str(err)) from err
        except UnexpectedResponse as err:
            await self._async_handle_unexpected(err)
            raise UpdateFailed(str(err)) from err
        if not profiles and await self._async_global_mode_off():
            raise UpdateFailed("Protect Alarm Manager is not in Global mode")

        for key in (ISSUE_NOT_SUPER_ADMIN, ISSUE_API_CHANGED, ISSUE_GLOBAL_MODE_OFF):
            self._delete_issue(key)
        self._unexpected_logged = False
        current = self.data or {}
        merged: dict[str, ArmProfile] = {}
        for profile in profiles:
            written = self._written_seq.get(profile.id, 0)
            if profile.id in current and written > seq_at_start:
                merged[profile.id] = current[profile.id]  # newer push/action wins
            else:
                merged[profile.id] = profile
        self.last_poll_success_at = dt_util.utcnow()
        self._check_profile_mapping(merged)
        self._check_push_issue()
        self._schedule_promotion(merged)
        return merged

    async def _async_global_mode_off(self) -> bool:
        try:
            console = await self.client.async_get_console_info()
        except UniFiAlarmError:
            return False
        if console.external_alarm_manager:
            self._delete_issue(ISSUE_GLOBAL_MODE_OFF)
            return False
        self._create_issue(ISSUE_GLOBAL_MODE_OFF)
        return True

    async def _async_handle_unexpected(self, err: UnexpectedResponse) -> None:
        self.last_unexpected_payload = err.payload
        if await self._async_global_mode_off():
            return
        if self._unexpected_logged:
            LOGGER.debug("Unexpected response from the Alarm Manager API: %s", err)
        else:
            self._unexpected_logged = True
            LOGGER.warning("Unexpected response from the Alarm Manager API: %s", err)
        LOGGER.debug(
            "Unexpected payload: %s", async_redact_data(err.payload, REDACT_KEYS)
        )
        self._create_issue(
            ISSUE_API_CHANGED,
            placeholders={
                "protect_version": self.console.protect_version or "unknown",
                "issue_url": ISSUE_TRACKER_URL,
            },
        )

    @callback
    def handle_profile(self, profile: ArmProfile) -> None:
        """Apply a pushed or action-returned profile immediately."""
        if self._shutdown_requested:
            return  # a push arriving during unload must not start new timers
        profile = _derive_missing_promotion_due_at(profile)
        self._write_seq += 1
        self._written_seq[profile.id] = self._write_seq
        data = dict(self.data or {})
        data[profile.id] = profile
        self._schedule_promotion(data)
        self.async_set_updated_data(data)

    @callback
    def handle_connection_change(self, connected: bool) -> None:
        """Slow polling while push works; poll fast and resync otherwise."""
        if connected:
            self._push_down_since = None
            self._delete_issue(ISSUE_PUSH_UNAVAILABLE)
            self.update_interval = POLL_INTERVAL_PUSH_HEALTHY
        else:
            self._push_down_since = dt_util.utcnow()
            self.update_interval = POLL_INTERVAL_PUSH_DOWN
        # Setting update_interval doesn't reschedule the pending timer; a refresh
        # does, and it also catches changes made while the socket was down.
        self.handle_resync()

    @callback
    def handle_resync(self) -> None:
        """Poll soon (debounced)."""
        if self._shutdown_requested:
            return
        self.config_entry.async_create_task(self.hass, self.async_request_refresh())

    @callback
    def handle_auth_failed(self) -> None:
        """The websocket's re-login failed: ask the user for new credentials."""
        self.config_entry.async_start_reauth(self.hass)

    async def async_arm_profile(self, profile_id: str) -> None:
        """Arm a profile and apply the console's response."""
        profile = await self._async_run_action(
            self.client.async_arm, profile_id, "arm_failed"
        )
        self.handle_profile(profile)

    async def async_disarm_profile(self, profile_id: str) -> None:
        """Disarm a profile and apply the console's response."""
        profile = await self._async_run_action(
            self.client.async_disarm, profile_id, "disarm_failed"
        )
        self.handle_profile(profile)

    async def _async_run_action(
        self,
        action: Callable[[str], Awaitable[ArmProfile]],
        profile_id: str,
        translation_key: str,
    ) -> ArmProfile:
        known = (self.data or {}).get(profile_id)
        title = known.title if known else profile_id
        try:
            return await action(profile_id)
        except UniFiAlarmError as err:
            if isinstance(err, (AuthFailed, MfaRequired)):
                self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=translation_key,
                translation_placeholders={"profile": title, "error": str(err)},
            ) from err

    def _check_profile_mapping(self, profiles: dict[str, ArmProfile]) -> None:
        mapped = [self.config_entry.options.get(key) for key in PROFILE_OPTION_KEYS]
        if any(profile_id and profile_id not in profiles for profile_id in mapped):
            self._create_issue(ISSUE_PROFILE_MISSING, fixable=True)
        else:
            self._delete_issue(ISSUE_PROFILE_MISSING)

    def _check_push_issue(self) -> None:
        if (
            self._push_down_since is not None
            and dt_util.utcnow() - self._push_down_since > PUSH_UNAVAILABLE_AFTER
        ):
            self._create_issue(
                ISSUE_PUSH_UNAVAILABLE,
                severity=ir.IssueSeverity.WARNING,
                placeholders={"issue_url": ISSUE_TRACKER_URL},
            )

    @callback
    def _schedule_promotion(self, profiles: dict[str, ArmProfile]) -> None:
        """Refresh once when the earliest exit delay should have ended."""
        self._cancel_promotion()
        if self._shutdown_requested:
            return
        due_times: list[datetime] = []
        for profile in profiles.values():
            if profile.state != STATE_ARMING or profile.state_promotion_due_at is None:
                self._overdue_counts.pop(profile.id, None)
                continue
            if self._overdue_counts.get(profile.id, 0) >= PROMOTION_MAX_OVERDUE:
                continue  # console clock skew or a stuck promotion: rely on polling
            due_times.append(profile.state_promotion_due_at)
        if not due_times:
            return
        seconds_left = (min(due_times) - dt_util.utcnow()).total_seconds()
        delay = max(
            max(seconds_left, 0) + PROMOTION_GRACE_SECONDS, PROMOTION_MIN_DELAY_SECONDS
        )
        self._promotion_unsub = async_call_later(
            self.hass, delay, self._handle_promotion_due
        )

    @callback
    def _handle_promotion_due(self, _now: datetime) -> None:
        self._promotion_unsub = None
        now = dt_util.utcnow()
        for profile in (self.data or {}).values():
            due = profile.state_promotion_due_at
            if profile.state == STATE_ARMING and due is not None and due <= now:
                self._overdue_counts[profile.id] = (
                    self._overdue_counts.get(profile.id, 0) + 1
                )
        # Not debounced: this must not be merged with other refresh requests.
        self.config_entry.async_create_task(self.hass, self.async_refresh())

    @callback
    def _cancel_promotion(self) -> None:
        if self._promotion_unsub is not None:
            self._promotion_unsub()
            self._promotion_unsub = None

    def _issue_id(self, key: str) -> str:
        return f"{key}_{self.config_entry.entry_id}"

    def _create_issue(
        self,
        key: str,
        *,
        fixable: bool = False,
        severity: ir.IssueSeverity = ir.IssueSeverity.ERROR,
        placeholders: dict[str, str] | None = None,
    ) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id(key),
            is_fixable=fixable,
            severity=severity,
            translation_key=key,
            translation_placeholders=placeholders,
            data={"entry_id": self.config_entry.entry_id} if fixable else None,
        )

    def _delete_issue(self, key: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(key))
