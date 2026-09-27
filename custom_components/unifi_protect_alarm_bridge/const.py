"""Constants for the UniFi Protect Alarm Bridge integration."""

from datetime import timedelta
import logging
from typing import Final

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME

DOMAIN: Final = "unifi_protect_alarm_bridge"
LOGGER = logging.getLogger(__package__)

CONF_PROFILE_AWAY: Final = "profile_away"
CONF_PROFILE_HOME: Final = "profile_home"
CONF_PROFILE_NIGHT: Final = "profile_night"
PROFILE_OPTION_KEYS: Final = (CONF_PROFILE_AWAY, CONF_PROFILE_HOME, CONF_PROFILE_NIGHT)

# Alarm Manager profile states (UniFi OS UI enum ARMED/ARMING/BREACHED/DISARMED).
STATE_DISARMED: Final = "disarmed"
STATE_ARMING: Final = "arming"
STATE_ARMED: Final = "armed"
STATE_BREACHED: Final = "breached"

# Polling is only a safety net for the websocket, so the intervals are fixed.
POLL_INTERVAL_PUSH_HEALTHY: Final = timedelta(seconds=60)
POLL_INTERVAL_PUSH_DOWN: Final = timedelta(seconds=10)
PUSH_UNAVAILABLE_AFTER: Final = timedelta(hours=1)

# One-off refresh when an exit delay ends (state_promotion_due_at).
PROMOTION_GRACE_SECONDS: Final = 2.0
PROMOTION_MIN_DELAY_SECONDS: Final = 5.0
PROMOTION_MAX_OVERDUE: Final = 3

ISSUE_NOT_SUPER_ADMIN: Final = "not_super_admin"
ISSUE_API_CHANGED: Final = "api_changed"
ISSUE_GLOBAL_MODE_OFF: Final = "global_mode_off"
ISSUE_PUSH_UNAVAILABLE: Final = "push_unavailable"
ISSUE_PROFILE_MISSING: Final = "profile_missing"
ALL_ISSUES: Final = (
    ISSUE_NOT_SUPER_ADMIN,
    ISSUE_API_CHANGED,
    ISSUE_GLOBAL_MODE_OFF,
    ISSUE_PUSH_UNAVAILABLE,
    ISSUE_PROFILE_MISSING,
)
ISSUE_TRACKER_URL: Final = (
    "https://github.com/violuke/ha_unifi_protect_alarm_bridge/issues"
)

# Keys removed from diagnostics and from logged API payloads. Profile titles and
# console names can contain people's names or addresses. Session tokens and
# cookies are also removed.
REDACT_KEYS: Final = {
    CONF_HOST,
    CONF_USERNAME,
    CONF_PASSWORD,
    "title",
    "profile_title",
    "name",
    "csrf_token",
    "csrfToken",
    "x-csrf-token",
    "X-CSRF-Token",
    "TOKEN",
    "token",
    "cookie",
    "Cookie",
}
