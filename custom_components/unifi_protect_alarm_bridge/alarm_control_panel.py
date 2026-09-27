"""Alarm control panel platform (entity added in the next task)."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import UniFiAlarmConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UniFiAlarmConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the platform."""
