"""Repair flows for UniFi Protect Alarm Bridge."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlow,
    RepairsFlowResult,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .config_flow import mapping_from_input, profiles_schema, validate_mapping
from .const import ISSUE_PROFILE_MISSING


class ProfileMissingRepairFlow(RepairsFlow):
    """Let the user choose arm profiles again after one was deleted."""

    def __init__(self, entry_id: str) -> None:
        """Remember which entry to fix."""
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Show the mapping form, then save it and reload the entry."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        profiles = list(entry.runtime_data.data.values())
        if not profiles:
            return self.async_abort(reason="no_profiles")
        errors: dict[str, str] = {}
        if user_input is not None and not (errors := validate_mapping(user_input)):
            self.hass.config_entries.async_update_entry(
                entry, options=mapping_from_input(user_input)
            )
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="init",
            data_schema=profiles_schema(profiles, user_input or entry.options),
            errors=errors,
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the fix flow for a fixable issue."""
    if (
        issue_id.startswith(ISSUE_PROFILE_MISSING)
        and data
        and isinstance(data.get("entry_id"), str)
    ):
        return ProfileMissingRepairFlow(data["entry_id"])
    return ConfirmRepairFlow()
