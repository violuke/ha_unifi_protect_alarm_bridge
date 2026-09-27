"""Diagnostics for UniFi Protect Alarm Bridge."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from .const import REDACT_KEYS
from .coordinator import UniFiAlarmConfigEntry


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: UniFiAlarmConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = entry.runtime_data
    listener = coordinator.listener
    console = coordinator.console
    interval = coordinator.update_interval
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "console": {
                "name": console.name,
                "model": console.model,
                "protect_version": console.protect_version,
                "unifi_os_version": console.firmware_version,
                "external_alarm_manager": console.external_alarm_manager,
            },
            "profiles": {
                profile_id: profile.as_dict()
                for profile_id, profile in (coordinator.data or {}).items()
            },
            "push": {
                "connected": listener.connected,
                "last_message_at": _iso(listener.last_message_at),
                "reconnect_count": listener.reconnect_count,
            },
            "polling": {
                "update_interval_seconds": (
                    interval.total_seconds() if interval else None
                ),
                "last_update_success": coordinator.last_update_success,
                "last_poll_success_at": _iso(coordinator.last_poll_success_at),
            },
            "last_unexpected_payload": coordinator.last_unexpected_payload,
        },
        REDACT_KEYS,
    )
