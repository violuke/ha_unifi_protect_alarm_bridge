"""Alarm control panel backed by UniFi Protect Alarm Manager profiles."""

from __future__ import annotations

from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ArmProfile
from .const import (
    CONF_PROFILE_AWAY,
    CONF_PROFILE_HOME,
    CONF_PROFILE_NIGHT,
    DOMAIN,
    LOGGER,
    PROFILE_OPTION_KEYS,
    STATE_ARMED,
    STATE_ARMING,
    STATE_BREACHED,
    STATE_DISARMED,
)
from .coordinator import UniFiAlarmConfigEntry, UniFiAlarmCoordinator

PARALLEL_UPDATES = 1  # serialise arm/disarm calls

MODE_STATES = {
    CONF_PROFILE_AWAY: AlarmControlPanelState.ARMED_AWAY,
    CONF_PROFILE_HOME: AlarmControlPanelState.ARMED_HOME,
    CONF_PROFILE_NIGHT: AlarmControlPanelState.ARMED_NIGHT,
}
MODE_FEATURES = {
    CONF_PROFILE_AWAY: AlarmControlPanelEntityFeature.ARM_AWAY,
    CONF_PROFILE_HOME: AlarmControlPanelEntityFeature.ARM_HOME,
    CONF_PROFILE_NIGHT: AlarmControlPanelEntityFeature.ARM_NIGHT,
}
# Which profile the panel reports when several are active. Unknown future states
# rank above "disarmed", so they surface rather than hide.
STATE_SEVERITY = {STATE_BREACHED: 4, STATE_ARMING: 3, STATE_ARMED: 2, STATE_DISARMED: 0}
UNKNOWN_STATE_SEVERITY = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UniFiAlarmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the panel for this console."""
    async_add_entities([UniFiAlarmPanel(entry.runtime_data)])


class UniFiAlarmPanel(
    CoordinatorEntity[UniFiAlarmCoordinator], AlarmControlPanelEntity
):
    """One panel per console, reflecting its most severe arm profile."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_code_arm_required = False

    def __init__(self, coordinator: UniFiAlarmCoordinator) -> None:
        """Build the entity from the entry's mapping and the console identity."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        console = coordinator.console
        unique_id = entry.unique_id or entry.entry_id
        self._attr_unique_id = unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unique_id)},
            connections={(CONNECTION_NETWORK_MAC, unique_id)},
            manufacturer="Ubiquiti",
            name=console.name,
            model=console.model,
            sw_version=console.protect_version,
            configuration_url=f"https://{entry.data[CONF_HOST]}",
        )
        self._mode_profiles = {
            mode: profile_id
            for mode in PROFILE_OPTION_KEYS
            if (profile_id := entry.options.get(mode))
        }
        self._profile_modes = {
            profile_id: mode for mode, profile_id in self._mode_profiles.items()
        }
        features = AlarmControlPanelEntityFeature(0)
        for mode in self._mode_profiles:
            features |= MODE_FEATURES[mode]
        self._attr_supported_features = features
        self._unknown_states_logged: set[str] = set()

    @property
    def _profiles(self) -> dict[str, ArmProfile]:
        return self.coordinator.data or {}

    def _effective_profile(self) -> ArmProfile | None:
        profiles = list(self._profiles.values())
        if not profiles:
            return None
        best = min(
            profiles,
            key=lambda profile: (
                -STATE_SEVERITY.get(profile.state, UNKNOWN_STATE_SEVERITY),
                profile.id not in self._profile_modes,
                profile.id,
            ),
        )
        if best.state == STATE_DISARMED:
            away_id = self._mode_profiles.get(CONF_PROFILE_AWAY)
            return self._profiles.get(away_id or "") or best
        return best

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Map the effective profile's state to an HA alarm state."""
        profile = self._effective_profile()
        if profile is None:
            return None
        if profile.state == STATE_DISARMED:
            return AlarmControlPanelState.DISARMED
        if profile.state == STATE_ARMING:
            return AlarmControlPanelState.ARMING
        if profile.state == STATE_BREACHED:
            return AlarmControlPanelState.TRIGGERED
        if profile.state == STATE_ARMED:
            mode = self._profile_modes.get(profile.id)
            return (
                MODE_STATES[mode]
                if mode
                else AlarmControlPanelState.ARMED_CUSTOM_BYPASS
            )
        if profile.state not in self._unknown_states_logged:
            self._unknown_states_logged.add(profile.state)
            LOGGER.warning(
                "Arm profile %s reported an unknown state %r", profile.id, profile.state
            )
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Details of the profile the state comes from."""
        profile = self._effective_profile()
        if profile is None:
            return None
        return {
            "profile_title": profile.title,
            "activation_delay": profile.activation_delay,
            "state_set_at": (
                profile.state_set_at.isoformat() if profile.state_set_at else None
            ),
            "state_promotion_due_at": (
                profile.state_promotion_due_at.isoformat()
                if profile.state_promotion_due_at
                else None
            ),
        }

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Arm the away profile."""
        await self._async_arm(CONF_PROFILE_AWAY)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Arm the home profile."""
        await self._async_arm(CONF_PROFILE_HOME)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Arm the night profile."""
        await self._async_arm(CONF_PROFILE_NIGHT)

    async def _async_arm(self, mode: str) -> None:
        profile_id = self._mode_profiles.get(mode)
        target = self._profiles.get(profile_id) if profile_id else None
        if target is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="profile_missing"
            )
        # Defined behaviour whether or not Protect allows two armed profiles.
        for other in list(self._profiles.values()):
            if other.id != target.id and other.state != STATE_DISARMED:
                await self.coordinator.async_disarm_profile(other.id)
        if target.state == STATE_BREACHED:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="disarm_first"
            )
        if target.state != STATE_DISARMED:
            return  # already arming/armed: a second press does nothing
        await self.coordinator.async_arm_profile(target.id)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Disarm every active profile, reporting any that fail."""
        failures: list[tuple[str, HomeAssistantError]] = []
        for profile in list(self._profiles.values()):
            if profile.state == STATE_DISARMED:
                continue
            try:
                await self.coordinator.async_disarm_profile(profile.id)
            except HomeAssistantError as err:
                failures.append((profile.title, err))
        if failures:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="disarm_failed",
                translation_placeholders={
                    "profile": ", ".join(title for title, _ in failures),
                    "error": "; ".join(
                        str(err.__cause__ or err) for _, err in failures
                    ),
                },
            )
