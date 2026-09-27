# UniFi Protect Alarm Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A HACS-installable Home Assistant custom integration exposing an `alarm_control_panel` for UniFi Protect's Global Alarm Manager. It reads and changes arm state through UniFi OS's `/api/v2/alarms/profiles` API and receives instant updates over Protect's `ws/updates` websocket.

**Architecture:**
- `api.py` is a pure-aiohttp client: login and CSRF handling, a login lock, and typed errors.
- `websocket.py` decodes Protect's binary update frames and forwards `externalArmProfile` updates.
- `coordinator.py` is a `DataUpdateCoordinator`. It merges pushes, action responses and polls, using a stale-write guard.
- `alarm_control_panel.py` maps the highest-severity profile to an HA alarm state.
- Everything user-facing goes through the config, options and repairs flows, with translations.

**Tech Stack:** Python 3.14, Home Assistant ≥ 2026.9, aiohttp (bundled with HA), pytest-homeassistant-custom-component, ruff, GitHub Actions (hassfest, hacs/action).

**Spec:** `docs/superpowers/specs/2026-09-27-unifi-protect-alarm-bridge-design.md`. Read it before starting; the plan argues from it.

## Global Constraints

- Domain `unifi_protect_alarm_bridge`; minimum Home Assistant `2026.9.0`; Python 3.14. Older HA releases are not supported; don't add compatibility shims for them.
- `manifest.json`: `iot_class: local_push`, `integration_type: hub`, `requirements: []`, `version: 0.1.0`, `codeowners: ["@violuke"]`.
- Never import `uiprotect` or the official `unifiprotect` integration.
- License: MIT.
- `api.py` and `websocket.py` must not import `homeassistant`.
- Every aiohttp session that talks to the console uses `aiohttp.CookieJar(unsafe=True)`. The console is usually reached by IP address, and the default jar silently drops cookies from IP hosts.
- **No real host, API key, username, password, MAC or profile ID may appear in any committed file.**
  - Tests use host `192.0.2.1` (or the local fake server), user `test-user`, password `test-password`, and `00000000-0000-4000-8000-00000000000X` IDs.
  - Real credentials for manual testing are typed into the HA UI of `scripts/develop`, which keeps them only in gitignored `config/.storage`.
- **Do not push tags or create releases** unless the maintainer explicitly asks. Do not push branches or change GitHub repo settings without asking the maintainer first.
- **Never call arm or disarm on the real console without asking the maintainer in that moment.**
- **Poll intervals are hard-coded, with no user option:**
  - 60 s while the websocket is healthy
  - 10 s while it is down
- **All user-facing text lives in `translations/en.json`.** There is no `strings.json`.
- **Commit trailer:** end every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Before each commit:** run `.venv/bin/ruff check --fix . && .venv/bin/ruff format .`. Import order and formatting in this plan's snippets are then normalised by the tool; don't hand-fix them.

## Review Focus

These five inputs are implied by the spec but not required by it. They are the most likely to bite a real user. Each has a pinned test in the task that owns the code.

1. **Host typed as a URL** (`https://192.168.1.1/`, trailing slash or path). Setup should store the bare `host[:port]` and work, rather than fail with `cannot_connect`. Covered in Task 4.
2. **IPv6 literal host** (`fe80::1`). It must be bracketed, as `[fe80::1]`, for URLs to work. Covered in Task 4.
3. **Activation delay switched off.** Arm returns `armed` immediately. The panel should show `armed_away` straight away, and no promotion timer should be left running. Covered in Tasks 5 and 7.
4. **HA restarts while the system is armed or breached.** The panel should come up showing `armed_away` or `triggered`, never flash `disarmed`. Covered in Task 6.
5. **Arm pressed twice** (double-tap while `arming`). The second press must not send another arm request. Covered in Task 7.

---

## File Structure

```
custom_components/unifi_protect_alarm_bridge/
  __init__.py              setup/unload/remove; builds session, client, coordinator
  manifest.json
  const.py                 constants, option keys, issue keys, timings, redact keys
  api.py                   UniFiAlarmClient, ArmProfile, ConsoleInfo, typed errors (no HA imports)
  websocket.py             decode_packet, ProtectUpdatesListener (no HA imports)
  coordinator.py           UniFiAlarmCoordinator, UniFiAlarmConfigEntry, issue helpers
  config_flow.py           user/profiles/reauth/reconfigure steps, options flow, shared mapping helpers
  alarm_control_panel.py   UniFiAlarmPanel entity
  diagnostics.py           redacted diagnostics
  repairs.py               profile_missing fix flow
  translations/en.json
  brand/icon.png  brand/icon@2x.png
tests/
  __init__.py  conftest.py  helpers.py  fake_console.py
  test_api.py  test_websocket.py  test_config_flow.py  test_coordinator.py
  test_init.py  test_alarm_control_panel.py  test_diagnostics.py  test_repairs.py
scripts/develop            local HA with the integration symlinked in
config/configuration.yaml  dev HA config (everything else in config/ is gitignored)
hacs.json  pyproject.toml  requirements_test.txt
.github/workflows/validate.yml  .github/workflows/release.yml
.gitignore  LICENSE  README.md  CONTRIBUTING.md  CLAUDE.md
```

---

### Task 1: Scaffold, packaging and CI that is green from the first commit

**Files:**
- Create: `custom_components/unifi_protect_alarm_bridge/{__init__.py,manifest.json,const.py,config_flow.py,translations/en.json}`
- Create: `hacs.json`, `pyproject.toml`, `requirements_test.txt`, `LICENSE`, `.github/workflows/validate.yml`
- Modify: `.gitignore`
- Test: `tests/__init__.py`, `tests/conftest.py`, `tests/test_smoke.py`

**Interfaces:**
- Consumes: nothing.
- Produces: every constant in `const.py` (used by all later tasks), the complete `translations/en.json`, and a stub `UniFiAlarmConfigFlow` that Task 4 replaces.

- [ ] **Step 1: Create the local test environment**

```bash
cd /Users/lukecousins/orca/ha_unifi_protect_alarm_bridge
cat > requirements_test.txt <<'EOF'
# Home Assistant 2026.9.4 (the minimum supported release) - Python 3.14
pytest-homeassistant-custom-component==0.13.367
ruff==0.16.9
EOF
uv venv .venv -p 3.14 && uv pip install --python .venv -r requirements_test.txt
```
Expected: the install succeeds. `.venv/bin/python -c "import homeassistant.const as c; print(c.__version__)"` prints `2026.9.4`.

- [ ] **Step 2: Replace `.gitignore`**

```gitignore
# Local secrets and captures
.env
.env.*
!.env.example
*.har
probe_output/

# Python
__pycache__/
*.py[cod]
.venv*/
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/

# Local Home Assistant dev instance (scripts/develop)
config/*
!config/configuration.yaml

.DS_Store
```

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
pythonpath = ["."]
testpaths = ["tests"]

[tool.ruff]
target-version = "py314"
line-length = 88

[tool.ruff.lint]
select = ["ASYNC", "B", "E", "F", "I", "RUF", "SIM", "UP", "W"]

[tool.ruff.lint.isort]
force-sort-within-sections = true
combine-as-imports = true
known-first-party = ["custom_components", "tests"]
```

- [ ] **Step 4: Write the failing smoke test**

`tests/__init__.py`:
```python
"""Tests for the UniFi Protect Alarm Bridge integration."""
```

`tests/conftest.py`:
```python
"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load integrations from custom_components/."""
```

`tests/test_smoke.py`:
```python
"""Smoke test proving the integration loads and its config flow starts."""

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.unifi_protect_alarm_bridge.const import DOMAIN


async def test_user_step_shows_form(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
```

- [ ] **Step 5: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_smoke.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'custom_components'`.

- [ ] **Step 6: Create the integration skeleton**

`custom_components/unifi_protect_alarm_bridge/manifest.json`:
```json
{
  "domain": "unifi_protect_alarm_bridge",
  "name": "UniFi Protect Alarm Bridge",
  "codeowners": ["@violuke"],
  "config_flow": true,
  "documentation": "https://github.com/violuke/ha_unifi_protect_alarm_bridge",
  "integration_type": "hub",
  "iot_class": "local_push",
  "issue_tracker": "https://github.com/violuke/ha_unifi_protect_alarm_bridge/issues",
  "requirements": [],
  "version": "0.1.0"
}
```

`custom_components/unifi_protect_alarm_bridge/const.py`:
```python
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
# console names can contain people's names or addresses.
REDACT_KEYS: Final = {
    CONF_HOST,
    CONF_USERNAME,
    CONF_PASSWORD,
    "title",
    "profile_title",
    "name",
}
```

`custom_components/unifi_protect_alarm_bridge/__init__.py` (stub, replaced in Task 6):
```python
"""UniFi Protect Alarm Bridge integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return True
```

`custom_components/unifi_protect_alarm_bridge/config_flow.py` (stub, replaced in Task 4):
```python
"""Config flow for UniFi Protect Alarm Bridge."""

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class UniFiAlarmConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the (not yet implemented) user step."""
        return self.async_show_form(step_id="user")
```

`custom_components/unifi_protect_alarm_bridge/translations/en.json` is the complete file. Later tasks only use these keys and never edit them.
```json
{
  "config": {
    "step": {
      "user": {
        "title": "Connect to your UniFi console",
        "description": "Use a dedicated local-only UniFi OS account with the Super Admin role and no two-factor authentication. Alarm Manager is not available to custom roles.",
        "data": {
          "host": "Host",
          "username": "Username",
          "password": "Password",
          "verify_ssl": "Verify SSL certificate"
        },
        "data_description": {
          "host": "IP address or hostname of the UniFi console running Protect.",
          "verify_ssl": "Leave this off unless the console has a certificate Home Assistant trusts."
        }
      },
      "profiles": {
        "title": "Map arm modes to Protect profiles",
        "description": "Choose which Alarm Manager profile each Home Assistant arm mode arms. Arm home and arm night are optional.",
        "data": {
          "profile_away": "Arm away",
          "profile_home": "Arm home",
          "profile_night": "Arm night"
        }
      },
      "reauth_confirm": {
        "title": "Re-authenticate with {host}",
        "description": "The console rejected the stored credentials. Enter the current username and password of the Super Admin account.",
        "data": {
          "username": "Username",
          "password": "Password"
        }
      },
      "reconfigure": {
        "title": "Reconfigure connection",
        "description": "Change how Home Assistant reaches the console. It must be the same console as before.",
        "data": {
          "host": "Host",
          "verify_ssl": "Verify SSL certificate"
        }
      }
    },
    "error": {
      "invalid_auth": "Invalid username or password.",
      "mfa_not_supported": "This account uses two-factor authentication. Use a local-only account without two-factor authentication.",
      "rate_limited": "Too many failed logins. Wait a few minutes and try again.",
      "cannot_connect": "Could not connect to the console.",
      "not_super_admin": "This account needs the Super Admin role. Alarm Manager is not available to custom roles.",
      "global_mode_off": "Protect's Alarm Manager is not in Global mode on this console.",
      "no_profiles": "No arm profiles found. Create one in Protect's Alarm Manager first.",
      "duplicate_profile": "Each profile can only be used for one arm mode.",
      "unknown": "Unexpected error. Check the Home Assistant logs."
    },
    "abort": {
      "already_configured": "This console is already configured.",
      "already_in_progress": "Setup for this console is already in progress.",
      "reauth_successful": "Re-authentication was successful.",
      "reconfigure_successful": "Reconfiguration was successful.",
      "unique_id_mismatch": "These details belong to a different console than the one originally configured."
    }
  },
  "options": {
    "step": {
      "init": {
        "title": "Arm mode mapping",
        "description": "Choose which Alarm Manager profile each Home Assistant arm mode arms. Arm home and arm night are optional.",
        "data": {
          "profile_away": "Arm away",
          "profile_home": "Arm home",
          "profile_night": "Arm night"
        }
      }
    },
    "error": {
      "duplicate_profile": "Each profile can only be used for one arm mode."
    },
    "abort": {
      "not_loaded": "The integration must be running to change the arm mode mapping."
    }
  },
  "issues": {
    "not_super_admin": {
      "title": "UniFi alarm account is not a Super Admin",
      "description": "The console refused access to Alarm Manager. Give the account used by UniFi Protect Alarm Bridge the Super Admin role again; custom roles cannot use Alarm Manager. This issue clears itself on the next successful poll."
    },
    "api_changed": {
      "title": "UniFi Alarm Manager returned unexpected data",
      "description": "The console (Protect {protect_version}) returned data this integration does not understand, probably after a firmware update. Please report it at {issue_url} and attach a diagnostics download."
    },
    "global_mode_off": {
      "title": "Protect Alarm Manager is not in Global mode",
      "description": "This integration controls Protect's Global Alarm Manager, but the console reports that Global mode is off. Switch Alarm Manager back to Global mode in Protect, or use the official UniFi Protect integration while in Local mode."
    },
    "push_unavailable": {
      "title": "Instant UniFi alarm updates are unavailable",
      "description": "The Protect update websocket has been disconnected for over an hour, so the alarm state is only refreshed every 10 seconds. This usually follows a firmware change. Please report it at {issue_url} if it persists."
    },
    "profile_missing": {
      "title": "Mapped UniFi arm profile no longer exists",
      "fix_flow": {
        "step": {
          "init": {
            "title": "Choose arm profiles again",
            "description": "A Protect arm profile used by Home Assistant was deleted or replaced. Choose which profile each arm mode should use.",
            "data": {
              "profile_away": "Arm away",
              "profile_home": "Arm home",
              "profile_night": "Arm night"
            }
          }
        },
        "error": {
          "duplicate_profile": "Each profile can only be used for one arm mode."
        },
        "abort": {
          "not_loaded": "The integration must be running to fix this."
        }
      }
    }
  },
  "exceptions": {
    "profile_missing": {
      "message": "The Protect arm profile mapped to this arm mode no longer exists. Fix it under Settings → Repairs."
    },
    "arm_failed": {
      "message": "Failed to arm {profile}: {error}"
    },
    "disarm_failed": {
      "message": "Failed to disarm {profile}: {error}"
    }
  }
}
```

- [ ] **Step 7: Run the smoke test**

Run: `.venv/bin/pytest -v`
Expected: `1 passed`.

- [ ] **Step 8: Add HACS metadata, the license, and the CI workflow**

`hacs.json`:
```json
{
  "name": "UniFi Protect Alarm Bridge",
  "content_in_root": false,
  "homeassistant": "2026.9.0",
  "render_readme": true,
  "zip_release": true,
  "filename": "unifi_protect_alarm_bridge.zip"
}
```

`LICENSE`:
```text
MIT License

Copyright (c) 2026 violuke

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

`.github/workflows/validate.yml`:
```yaml
name: Validate

on:
  push:
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  hassfest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: home-assistant/actions/hassfest@master

  hacs:
    runs-on: ubuntu-latest
    steps:
      - uses: hacs/action@main
        with:
          category: integration
          # Brand images ship locally in custom_components/<domain>/brand/, because
          # home-assistant/brands no longer accepts custom integrations.
          ignore: brands

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.14"
      - run: pip install ruff==0.16.9
      - run: ruff check .
      - run: ruff format --check .

  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.14"
      - run: pip install -r requirements_test.txt
      - run: pytest
```

- [ ] **Step 9: Lint and commit**

```bash
.venv/bin/ruff check --fix . && .venv/bin/ruff format . && .venv/bin/pytest -q
git add .gitignore pyproject.toml requirements_test.txt hacs.json LICENSE \
  .github/workflows/validate.yml custom_components tests
git commit -m "chore: scaffold integration, packaging and CI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 10: Get CI running (ask first)**

Ask the maintainer:
1. "May I push `feature/initial-integration` to origin so the Validate workflow runs?"
2. "`hacs/action` checks that the GitHub repo has a description and topics. May I run `gh repo edit violuke/ha_unifi_protect_alarm_bridge --description "Home Assistant alarm panel for UniFi Protect's Global Alarm Manager" --add-topic home-assistant --add-topic hacs --add-topic hacs-integration --add-topic unifi-protect`?"

Only on a yes: `git push -u origin feature/initial-integration`, then `gh run watch --exit-status`.

Expected: the hassfest, hacs, lint and tests jobs all pass. If `hacs` fails only on `description`/`topics` because the maintainer declined step 2, report that; don't add those checks to `ignore`.

---

### Task 2: `api.py` — REST client, models and typed errors

**Files:**
- Create: `custom_components/unifi_protect_alarm_bridge/api.py`
- Create: `tests/helpers.py`, `tests/fake_console.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: nothing from earlier tasks. `api.py` must not import `homeassistant`.
- Produces:
  - Errors, all subclasses of `UniFiAlarmError(Exception)`: `AuthFailed`, `MfaRequired`, `RateLimited`, `InsufficientPermissions`, `CannotConnect`, and `UnexpectedResponse(message: str, payload: Any = None)`, which exposes `.payload`.
  - `@dataclass(frozen=True, slots=True) ArmProfile`:
    - Fields: `id: str`, `title: str`, `state: str`, `activation_delay: int | None`, `state_set_at: datetime | None`, `state_promotion_due_at: datetime | None`. The datetimes are tz-aware UTC.
    - `ArmProfile.from_api(data: Any) -> ArmProfile`
    - `ArmProfile.as_dict() -> dict[str, Any]`, which gives ISO strings for the datetimes.
  - `@dataclass(frozen=True, slots=True) ConsoleInfo`:
    - Fields: `mac: str`, `name: str`, `model: str | None`, `protect_version: str | None`, `firmware_version: str | None` (UniFi OS), `external_alarm_manager: bool`.
    - `ConsoleInfo.from_api(data: Any)`.
  - `UniFiAlarmClient(session: aiohttp.ClientSession, host: str, username: str, password: str, *, scheme: str = "https")`:
    - Properties: `session`, `csrf_token: str | None`, `generation: int`.
    - `ws_url(path: str = UPDATES_WS_PATH) -> str`
    - `async_login(*, stale_generation: int | None = None) -> None`
    - `async_get_console_info() -> ConsoleInfo`
    - `async_get_profiles() -> list[ArmProfile]`
    - `async_arm(profile_id: str) -> ArmProfile`
    - `async_disarm(profile_id: str) -> ArmProfile`
  - Test helpers:
    - Constants: `HOST`, `USERNAME`, `PASSWORD`, `MAC`, `UNIQUE_ID`, `AWAY_ID`, `HOME_ID`, `OTHER_ID`, `ENTITY_ID`, `CONSOLE_JSON`, `CONSOLE`, `ENTRY_DATA`.
    - Functions: `profile_json(...)`, `make_profile(...)`, `titled_profile(profile_id, state)`, `encode_packet(...)`, `profile_packet(...)`, `mock_config_entry(options=None)`.
    - Fixtures: `fake_console`, `http_session`, `api_client`.

- [ ] **Step 1: Write the test data helpers**

`tests/helpers.py`:
```python
"""Shared test data. Every value is a placeholder, never real console data."""

from __future__ import annotations

from datetime import datetime
import json
import struct
from typing import Any
import zlib

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_protect_alarm_bridge.api import ArmProfile, ConsoleInfo
from custom_components.unifi_protect_alarm_bridge.const import CONF_PROFILE_AWAY, DOMAIN

HOST = "192.0.2.1"
USERNAME = "test-user"
PASSWORD = "test-password"
MAC = "AABBCCDDEEFF"
UNIQUE_ID = "aa:bb:cc:dd:ee:ff"
AWAY_ID = "00000000-0000-4000-8000-000000000001"
HOME_ID = "00000000-0000-4000-8000-000000000002"
OTHER_ID = "00000000-0000-4000-8000-000000000003"
TITLES = {AWAY_ID: "Away", HOME_ID: "Home", OTHER_ID: "Other"}
ENTITY_ID = "alarm_control_panel.test_console"

# Shape of GET /proxy/protect/api/nvr, trimmed to the fields we use.
CONSOLE_JSON: dict[str, Any] = {
    "id": "000000000000000000000001",
    "mac": MAC,
    "name": "Test Console",
    "type": "UDM-PRO",
    "version": "7.2.105",
    "firmwareVersion": "5.1.33",
    "featureFlags": {"useExternalAlarmManager": True},
}
CONSOLE = ConsoleInfo.from_api(CONSOLE_JSON)

ENTRY_DATA = {
    CONF_HOST: HOST,
    CONF_USERNAME: USERNAME,
    CONF_PASSWORD: PASSWORD,
    CONF_VERIFY_SSL: False,
}


def profile_json(
    profile_id: str = AWAY_ID,
    title: str | None = None,
    state: str = "disarmed",
    activation_delay: int | None = 60,
    promotion_due: datetime | None = None,
) -> dict[str, Any]:
    """Return a profile in the shape of GET /api/v2/alarms/profiles."""
    data: dict[str, Any] = {
        "id": profile_id,
        "title": title or TITLES.get(profile_id, "Profile"),
        "activation_delay": activation_delay,
        "alarm_ids": ["00000000-0000-4000-8000-0000000000a1"],
        "created_at": "2026-09-11T00:17:58Z",
        "updated_at": "2026-09-11T00:17:58Z",
        "state": state,
        "state_set_at": "2026-09-27T13:55:44Z",
    }
    if promotion_due is not None:
        data["state_promotion_due_at"] = promotion_due.isoformat().replace(
            "+00:00", "Z"
        )
    return data


def make_profile(**kwargs: Any) -> ArmProfile:
    """Return a parsed ArmProfile (same kwargs as profile_json)."""
    return ArmProfile.from_api(profile_json(**kwargs))


def titled_profile(profile_id: str, state: str) -> ArmProfile:
    """Return the profile the fake API would answer an action with."""
    return make_profile(profile_id=profile_id, state=state)


def encode_packet(action: Any, data: Any, *, deflate: bool = False) -> bytes:
    """Encode two JSON frames in Protect's binary websocket format."""
    packet = b""
    for packet_type, frame in ((1, action), (2, data)):
        payload = json.dumps(frame).encode()
        if deflate:
            payload = zlib.compress(payload)
        packet += struct.pack(">BBBBI", packet_type, 1, int(deflate), 0, len(payload))
        packet += payload
    return packet


def profile_packet(profile: dict[str, Any], *, deflate: bool = False) -> bytes:
    """Encode an externalArmProfile update as Protect sends it."""
    action = {
        "action": "update",
        "newUpdateId": "00000000-0000-4000-8000-0000000000f1",
        "modelKey": "externalArmProfile",
        "id": profile["id"],
    }
    return encode_packet(action, profile, deflate=deflate)


def mock_config_entry(options: dict[str, str] | None = None) -> MockConfigEntry:
    """Return a config entry for the placeholder console."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test Console",
        unique_id=UNIQUE_ID,
        data=dict(ENTRY_DATA),
        options=options if options is not None else {CONF_PROFILE_AWAY: AWAY_ID},
    )
```

- [ ] **Step 2: Write the fake console (a real aiohttp server)**

`tests/fake_console.py`:
```python
"""A small fake UniFi console, so the real HTTP client can be tested end to end."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from .helpers import CONSOLE_JSON, PASSWORD, USERNAME, profile_json


def _jwt(claims: dict[str, Any]) -> str:
    def segment(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'HS256', 'typ': 'JWT'})}.{segment(claims)}.signature"


class FakeConsole:
    """In-memory UniFi OS login, Protect nvr/websocket and Alarm Manager API."""

    def __init__(self) -> None:
        self.username = USERNAME
        self.password = PASSWORD
        self.super_admin = True
        self.login_status: int | None = None  # force a login status, e.g. 429 or 499
        self.send_csrf_header = True
        self.jwt_tokens = False
        self.updated_csrf_on_get: str | None = None
        self.profiles: list[dict[str, Any]] = [profile_json()]
        self.profiles_payload: Any = None  # replaces the GET /profiles body
        self.nvr: dict[str, Any] = json.loads(json.dumps(CONSOLE_JSON))
        self.login_count = 0
        self.ws_connections = 0
        self.ws_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._sessions: dict[str, str] = {}  # TOKEN cookie -> expected CSRF token
        self._open_sockets: set[web.WebSocketResponse] = set()
        self.server: TestServer | None = None

    @property
    def host(self) -> str:
        """host:port of the running server."""
        assert self.server is not None
        return f"127.0.0.1:{self.server.port}"

    async def start(self) -> None:
        """Start listening on 127.0.0.1."""
        app = web.Application()
        app.router.add_post("/api/auth/login", self._login)
        app.router.add_get("/proxy/protect/api/nvr", self._nvr)
        app.router.add_get("/api/v2/alarms/profiles", self._get_profiles)
        app.router.add_post(
            "/api/v2/alarms/profiles/{profile_id}/actions/{verb}", self._action
        )
        app.router.add_get("/proxy/protect/ws/updates", self._ws)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()

    async def stop(self) -> None:
        """Close open websockets, then the server."""
        for ws in list(self._open_sockets):
            await ws.close()
        if self.server is not None:
            await self.server.close()

    def expire_sessions(self) -> None:
        """Invalidate every TOKEN cookie, as a console reboot would."""
        self._sessions.clear()

    def rotate_csrf(self) -> None:
        """Make every session expect a CSRF token the client doesn't have."""
        for token in self._sessions:
            self._sessions[token] = "rotated"

    def _token(self, request: web.Request) -> str | None:
        token = request.cookies.get("TOKEN")
        return token if token in self._sessions else None

    async def _login(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.login_count += 1
        if self.login_status is not None:
            return web.json_response({"code": "FORCED"}, status=self.login_status)
        if body.get("username") != self.username or body.get("password") != self.password:
            return web.json_response(
                {"error": {"code": 403, "message": "Forbidden"}}, status=403
            )
        csrf = f"csrf-{self.login_count}"
        token = _jwt({"csrfToken": csrf}) if self.jwt_tokens else f"token-{self.login_count}"
        self._sessions[token] = csrf
        headers = {"X-CSRF-Token": csrf} if self.send_csrf_header else {}
        response = web.json_response({"unique_id": "fake-user"}, headers=headers)
        response.set_cookie("TOKEN", token)
        return response

    async def _nvr(self, request: web.Request) -> web.Response:
        if self._token(request) is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(self.nvr)

    async def _get_profiles(self, request: web.Request) -> web.Response:
        token = self._token(request)
        if token is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        if not self.super_admin:
            return web.Response(text="Forbidden", status=403)
        headers = {}
        if self.updated_csrf_on_get:
            self._sessions[token] = self.updated_csrf_on_get
            headers["X-Updated-CSRF-Token"] = self.updated_csrf_on_get
        payload = self.profiles if self.profiles_payload is None else self.profiles_payload
        return web.json_response(payload, headers=headers)

    async def _action(self, request: web.Request) -> web.Response:
        token = self._token(request)
        if token is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.headers.get("X-CSRF-Token") != self._sessions[token]:
            return web.Response(text="Forbidden", status=403)
        if not self.super_admin:
            return web.Response(text="Forbidden", status=403)
        profile_id = request.match_info["profile_id"]
        verb = request.match_info["verb"]
        for profile in self.profiles:
            if profile["id"] == profile_id:
                profile["state"] = "arming" if verb == "arm" else "disarmed"
                return web.json_response(profile)
        return web.json_response({"error": "not found"}, status=404)

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        if self._token(request) is None:
            raise web.HTTPUnauthorized
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_connections += 1
        self._open_sockets.add(ws)
        pump = asyncio.create_task(self._pump(ws))
        try:
            async for _message in ws:
                pass
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            self._open_sockets.discard(ws)
        return ws

    async def _pump(self, ws: web.WebSocketResponse) -> None:
        while True:
            item = await self.ws_queue.get()
            if item is None:
                await ws.close()
                return
            await ws.send_bytes(item)
```

Replace `tests/conftest.py` with:
```python
"""Shared fixtures."""

from collections.abc import AsyncGenerator

import aiohttp
import pytest

from custom_components.unifi_protect_alarm_bridge.api import UniFiAlarmClient

from .fake_console import FakeConsole
from .helpers import PASSWORD, USERNAME


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load integrations from custom_components/."""


@pytest.fixture
async def fake_console(socket_enabled) -> AsyncGenerator[FakeConsole]:
    """A running fake console on 127.0.0.1 (sockets are otherwise blocked)."""
    console = FakeConsole()
    await console.start()
    yield console
    await console.stop()


@pytest.fixture
async def http_session() -> AsyncGenerator[aiohttp.ClientSession]:
    """A session configured exactly like the integration's (unsafe cookie jar)."""
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    yield session
    await session.close()


@pytest.fixture
def api_client(
    fake_console: FakeConsole, http_session: aiohttp.ClientSession
) -> UniFiAlarmClient:
    """A real client pointed at the fake console over plain HTTP."""
    return UniFiAlarmClient(
        http_session, fake_console.host, USERNAME, PASSWORD, scheme="http"
    )
```

- [ ] **Step 3: Write the failing API tests**

`tests/test_api.py`:
```python
"""Tests for the Alarm Manager HTTP client, run against a real local server."""

import asyncio
from datetime import UTC, datetime

import aiohttp
import pytest

from custom_components.unifi_protect_alarm_bridge.api import (
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

from .fake_console import FakeConsole
from .helpers import AWAY_ID, PASSWORD, USERNAME, profile_json


async def test_get_profiles_parses_profiles(api_client, fake_console) -> None:
    due = datetime(2026, 9, 27, 13, 56, 44, tzinfo=UTC)
    fake_console.profiles = [profile_json(state="arming", promotion_due=due)]

    profiles = await api_client.async_get_profiles()

    assert profiles == [
        ArmProfile(
            id=AWAY_ID,
            title="Away",
            state="arming",
            activation_delay=60,
            state_set_at=datetime(2026, 9, 27, 13, 55, 44, tzinfo=UTC),
            state_promotion_due_at=due,
        )
    ]
    assert fake_console.login_count == 1


def test_parses_seven_digit_fractional_seconds() -> None:
    """The console sends 100ns precision, e.g. 13:56:44.5852528Z."""
    data = {**profile_json(), "state_promotion_due_at": "2026-09-27T13:56:44.5852528Z"}
    profile = ArmProfile.from_api(data)
    assert profile.state_promotion_due_at == datetime(
        2026, 9, 27, 13, 56, 44, 585252, tzinfo=UTC
    )


def test_as_dict_is_json_safe() -> None:
    profile = ArmProfile.from_api(profile_json())
    assert profile.as_dict() == {
        "id": AWAY_ID,
        "title": "Away",
        "state": "disarmed",
        "activation_delay": 60,
        "state_set_at": "2026-09-27T13:55:44+00:00",
        "state_promotion_due_at": None,
    }


async def test_default_cookie_jar_cannot_hold_an_ip_session(fake_console) -> None:
    """Documents why the integration must use aiohttp.CookieJar(unsafe=True)."""
    async with aiohttp.ClientSession() as session:
        client = UniFiAlarmClient(
            session, fake_console.host, USERNAME, PASSWORD, scheme="http"
        )
        with pytest.raises(UnexpectedResponse):
            await client.async_get_profiles()


async def test_console_info(api_client) -> None:
    assert await api_client.async_get_console_info() == ConsoleInfo(
        mac="AABBCCDDEEFF",
        name="Test Console",
        model="UDM-PRO",
        protect_version="7.2.105",
        firmware_version="5.1.33",
        external_alarm_manager=True,
    )


async def test_console_info_global_mode_off(api_client, fake_console) -> None:
    fake_console.nvr["featureFlags"] = {"useExternalAlarmManager": False}
    info = await api_client.async_get_console_info()
    assert info.external_alarm_manager is False


async def test_console_info_without_mac_is_unexpected(api_client, fake_console) -> None:
    del fake_console.nvr["mac"]
    with pytest.raises(UnexpectedResponse):
        await api_client.async_get_console_info()


async def test_wrong_password_is_auth_failed(api_client, fake_console) -> None:
    """The real console answers a wrong password with 403, not 401."""
    fake_console.password = "something-else"
    with pytest.raises(AuthFailed):
        await api_client.async_login()


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, AuthFailed),
        (401, AuthFailed),
        (403, AuthFailed),
        (429, RateLimited),
        (499, MfaRequired),
        (500, UnexpectedResponse),
    ],
)
async def test_login_status_mapping(api_client, fake_console, status, error) -> None:
    fake_console.login_status = status
    with pytest.raises(error):
        await api_client.async_get_profiles()


async def test_expired_session_logs_in_again_once(api_client, fake_console) -> None:
    await api_client.async_get_profiles()
    fake_console.expire_sessions()

    await api_client.async_get_profiles()

    assert fake_console.login_count == 2


async def test_concurrent_expiry_logs_in_once(api_client, fake_console) -> None:
    await api_client.async_get_profiles()
    fake_console.expire_sessions()

    await asyncio.gather(*(api_client.async_get_profiles() for _ in range(3)))

    assert fake_console.login_count == 2


async def test_not_super_admin_after_fresh_login(api_client, fake_console) -> None:
    fake_console.super_admin = False
    with pytest.raises(InsufficientPermissions):
        await api_client.async_get_profiles()
    # The 403 is only trusted after one fresh login.
    assert fake_console.login_count == 2


async def test_arm_and_disarm_return_the_updated_profile(api_client) -> None:
    assert (await api_client.async_arm(AWAY_ID)).state == "arming"
    assert (await api_client.async_disarm(AWAY_ID)).state == "disarmed"


async def test_arm_unknown_profile_is_unexpected(api_client) -> None:
    with pytest.raises(UnexpectedResponse):
        await api_client.async_arm("00000000-0000-4000-8000-0000000000ff")


async def test_stale_csrf_on_post_logs_in_again(api_client, fake_console) -> None:
    await api_client.async_get_profiles()
    fake_console.rotate_csrf()

    assert (await api_client.async_arm(AWAY_ID)).state == "arming"
    assert fake_console.login_count == 2


async def test_updated_csrf_header_is_adopted(api_client, fake_console) -> None:
    fake_console.updated_csrf_on_get = "csrf-updated"
    await api_client.async_get_profiles()
    assert api_client.csrf_token == "csrf-updated"

    await api_client.async_arm(AWAY_ID)

    assert fake_console.login_count == 1


async def test_csrf_falls_back_to_the_jwt_claim(api_client, fake_console) -> None:
    fake_console.send_csrf_header = False
    fake_console.jwt_tokens = True

    await api_client.async_arm(AWAY_ID)

    assert api_client.csrf_token == "csrf-1"


async def test_login_without_any_csrf_is_unexpected(api_client, fake_console) -> None:
    fake_console.send_csrf_header = False
    with pytest.raises(UnexpectedResponse):
        await api_client.async_login()


@pytest.mark.parametrize(
    "payload",
    [
        {"not": "a list"},
        [{"id": AWAY_ID}],
        [{**profile_json(), "state_set_at": "yesterday"}],
        [{**profile_json(), "activation_delay": "60"}],
        [{**profile_json(), "state": None}],
    ],
)
async def test_schema_drift_is_unexpected(api_client, fake_console, payload) -> None:
    fake_console.profiles_payload = payload
    with pytest.raises(UnexpectedResponse) as err:
        await api_client.async_get_profiles()
    assert err.value.payload is not None


async def test_cannot_connect(http_session, socket_enabled) -> None:
    client = UniFiAlarmClient(http_session, "127.0.0.1:9", USERNAME, PASSWORD, scheme="http")
    with pytest.raises(CannotConnect):
        await client.async_login()


async def test_ws_url_follows_scheme(http_session) -> None:
    secure = UniFiAlarmClient(http_session, "192.0.2.1", USERNAME, PASSWORD)
    plain = UniFiAlarmClient(http_session, "127.0.0.1:1", USERNAME, PASSWORD, scheme="http")
    assert secure.ws_url() == "wss://192.0.2.1/proxy/protect/ws/updates"
    assert plain.ws_url() == "ws://127.0.0.1:1/proxy/protect/ws/updates"


async def test_fake_console_is_isolated_per_test(fake_console: FakeConsole) -> None:
    assert fake_console.login_count == 0
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'custom_components.unifi_protect_alarm_bridge.api'`.

- [ ] **Step 5: Implement `api.py`**

`custom_components/unifi_protect_alarm_bridge/api.py`:
```python
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
        if delay is not None and (not isinstance(delay, int) or isinstance(delay, bool)):
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
            "state_set_at": self.state_set_at.isoformat() if self.state_set_at else None,
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
            except (binascii.Error, ValueError):
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
        if self._csrf_token is None:
            await self.async_login()
        status, generation, payload = await self._send(method, path)
        if status in (401, 403):
            # 401: session expired. 403: possibly a stale CSRF token. Either way,
            # only trust the answer after one fresh login.
            await self.async_login(stale_generation=generation)
            status, _, payload = await self._send(method, path)
        if status == 401:
            raise UnexpectedResponse(
                f"{method} {path} is still unauthorised after a fresh login", payload
            )
        if status == 403:
            raise InsufficientPermissions(f"{method} {path} is forbidden for this account")
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
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: all tests PASS.

- [ ] **Step 7: Run everything, lint, and commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check --fix . && .venv/bin/ruff format .
git add custom_components tests
git commit -m "feat: add Alarm Manager API client with typed errors

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `websocket.py` — Protect update listener

**Files:**
- Create: `custom_components/unifi_protect_alarm_bridge/websocket.py`
- Test: `tests/test_websocket.py`

**Interfaces:**
- Consumes (from Task 2): `UniFiAlarmClient`, which provides `.session`, `.csrf_token`, `.generation`, `.ws_url()` and `.async_login(stale_generation=...)`. Also `ArmProfile.from_api`, and the errors `AuthFailed`, `MfaRequired`, `CannotConnect`, `RateLimited` and `UnexpectedResponse`.
- Produces:
  - `PacketDecodeError(ValueError)`
  - `decode_packet(data: bytes) -> list[Any]`
  - `PROFILE_MODEL_KEY = "externalArmProfile"`
  - `ProtectUpdatesListener(client, *, on_profile: Callable[[ArmProfile], None], on_connection_change: Callable[[bool], None], on_resync: Callable[[], None], on_auth_failed: Callable[[], None], heartbeat: float = 30.0, silence_timeout: float = 120.0, backoff_initial: float = 1.0, backoff_max: float = 300.0)`
    - Attributes: `connected: bool`, `last_message_at: datetime | None`, `reconnect_count: int`.
    - `async run() -> None` never returns, except after an auth failure or cancellation.

- [ ] **Step 1: Write the failing tests**

`tests/test_websocket.py`:
```python
"""Tests for the Protect update websocket listener."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
import contextlib
import struct
from typing import Any

import pytest

from custom_components.unifi_protect_alarm_bridge.api import ArmProfile, UniFiAlarmClient
from custom_components.unifi_protect_alarm_bridge.websocket import (
    PacketDecodeError,
    ProtectUpdatesListener,
    decode_packet,
)

from .helpers import AWAY_ID, PASSWORD, USERNAME, encode_packet, profile_json, profile_packet


@pytest.mark.parametrize("deflate", [False, True])
def test_decode_json_frames(deflate: bool) -> None:
    packet = encode_packet({"action": "update"}, {"a": 1}, deflate=deflate)
    assert decode_packet(packet) == [{"action": "update"}, {"a": 1}]


def test_decode_utf8_frame() -> None:
    payload = b"hello"
    packet = struct.pack(">BBBBI", 1, 2, 0, 0, len(payload)) + payload
    assert decode_packet(packet) == ["hello"]


@pytest.mark.parametrize(
    "packet",
    [
        b"\x01\x01\x00",
        encode_packet({"a": 1}, {"b": 2})[:-3],
        struct.pack(">BBBBI", 1, 1, 1, 0, 3) + b"xyz",  # "deflated" but not zlib
    ],
)
def test_decode_rejects_bad_packets(packet: bytes) -> None:
    with pytest.raises(PacketDecodeError):
        decode_packet(packet)


class Recorder:
    """Collects listener callbacks."""

    def __init__(self) -> None:
        self.profiles: list[ArmProfile] = []
        self.connection: list[bool] = []
        self.resyncs = 0
        self.auth_failed = 0

    def on_profile(self, profile: ArmProfile) -> None:
        self.profiles.append(profile)

    def on_connection_change(self, connected: bool) -> None:
        self.connection.append(connected)

    def on_resync(self) -> None:
        self.resyncs += 1

    def on_auth_failed(self) -> None:
        self.auth_failed += 1


async def wait_until(predicate: Callable[[], bool], limit: float = 5) -> None:
    async with asyncio.timeout(limit):
        while not predicate():  # noqa: ASYNC110 - polling test callbacks is intended
            await asyncio.sleep(0.01)


@pytest.fixture
async def start_listener() -> AsyncGenerator[
    Callable[..., tuple[ProtectUpdatesListener, Recorder, asyncio.Task[None]]]
]:
    tasks: list[asyncio.Task[None]] = []

    def _start(
        client: UniFiAlarmClient, **kwargs: Any
    ) -> tuple[ProtectUpdatesListener, Recorder, asyncio.Task[None]]:
        recorder = Recorder()
        options = {"backoff_initial": 0.01, "backoff_max": 0.05, **kwargs}
        listener = ProtectUpdatesListener(
            client,
            on_profile=recorder.on_profile,
            on_connection_change=recorder.on_connection_change,
            on_resync=recorder.on_resync,
            on_auth_failed=recorder.on_auth_failed,
            **options,
        )
        task = asyncio.create_task(listener.run())
        tasks.append(task)
        return listener, recorder, task

    yield _start
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("deflate", [False, True])
async def test_forwards_profile_updates(
    api_client, fake_console, start_listener, deflate
) -> None:
    listener, recorder, _ = start_listener(api_client)
    await wait_until(lambda: recorder.connection == [True])

    await fake_console.ws_queue.put(
        profile_packet(profile_json(state="arming"), deflate=deflate)
    )
    await wait_until(lambda: len(recorder.profiles) == 1)

    assert recorder.profiles[0].id == AWAY_ID
    assert recorder.profiles[0].state == "arming"
    assert listener.connected is True
    assert listener.last_message_at is not None


async def test_ignores_other_models_and_garbage(
    api_client, fake_console, start_listener
) -> None:
    _, recorder, _ = start_listener(api_client)
    await wait_until(lambda: recorder.connection == [True])

    await fake_console.ws_queue.put(
        encode_packet(
            {"action": "update", "modelKey": "sensor", "id": "x"},
            {"isMotionDetected": True},
        )
    )
    await fake_console.ws_queue.put(b"\x00\x01")
    await fake_console.ws_queue.put(profile_packet(profile_json(state="armed")))
    await wait_until(lambda: len(recorder.profiles) == 1)

    assert recorder.profiles[0].state == "armed"
    assert recorder.resyncs == 0


async def test_partial_profile_update_requests_resync(
    api_client, fake_console, start_listener
) -> None:
    _, recorder, _ = start_listener(api_client)
    await wait_until(lambda: recorder.connection == [True])

    await fake_console.ws_queue.put(profile_packet({"id": AWAY_ID, "state": "armed"}))
    await wait_until(lambda: recorder.resyncs == 1)

    assert recorder.profiles == []


async def test_reconnects_after_server_closes(
    api_client, fake_console, start_listener
) -> None:
    listener, recorder, _ = start_listener(api_client)
    await wait_until(lambda: recorder.connection == [True])

    await fake_console.ws_queue.put(None)  # server closes the socket
    await wait_until(lambda: recorder.connection == [True, False, True])

    assert fake_console.ws_connections == 2
    assert listener.reconnect_count >= 1


async def test_silent_socket_is_treated_as_dead(
    api_client, fake_console, start_listener, caplog
) -> None:
    _, recorder, _ = start_listener(api_client, silence_timeout=0.2)
    await wait_until(lambda: recorder.connection.count(True) >= 3)

    assert fake_console.ws_connections >= 3
    warnings = [r for r in caplog.records if "disconnected" in r.getMessage()]
    assert len(warnings) == 1  # repeat silence drops are logged at debug only


async def test_mfa_challenge_stops_the_listener(
    api_client, fake_console, start_listener
) -> None:
    await api_client.async_login()
    fake_console.expire_sessions()
    fake_console.login_status = 499

    _, recorder, task = start_listener(api_client)
    await wait_until(task.done)

    assert recorder.auth_failed == 1


async def test_rate_limited_login_backs_off_and_retries(
    api_client, fake_console, start_listener
) -> None:
    fake_console.login_status = 429
    listener, recorder, task = start_listener(api_client)

    await wait_until(lambda: listener.reconnect_count >= 2)
    assert not task.done()
    assert recorder.auth_failed == 0

    fake_console.login_status = None
    await wait_until(lambda: recorder.connection == [True])


async def test_callback_error_does_not_kill_the_listener(
    api_client, fake_console, start_listener
) -> None:
    listener, recorder, task = start_listener(api_client)
    await wait_until(lambda: recorder.connection == [True])
    calls = 0

    def exploding_on_profile(profile: ArmProfile) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("bug in a callback")

    listener._on_profile = exploding_on_profile
    await fake_console.ws_queue.put(profile_packet(profile_json(state="armed")))

    await wait_until(lambda: recorder.connection == [True, False, True])
    assert calls == 1
    assert not task.done()


async def test_logs_in_again_when_handshake_is_unauthorised(
    api_client, fake_console, start_listener
) -> None:
    await api_client.async_login()
    fake_console.expire_sessions()

    _, recorder, _ = start_listener(api_client)
    await wait_until(lambda: recorder.connection == [True])

    assert fake_console.login_count == 2


async def test_auth_failure_stops_the_listener(
    api_client, fake_console, start_listener
) -> None:
    await api_client.async_login()
    fake_console.expire_sessions()
    fake_console.password = "changed-by-the-user"

    _, recorder, task = start_listener(api_client)
    await wait_until(task.done)

    assert recorder.auth_failed == 1
    assert recorder.connection == []
    assert fake_console.login_count == 2  # exactly one failed retry, no loop


async def test_keeps_retrying_when_console_is_unreachable(
    http_session, socket_enabled, start_listener
) -> None:
    client = UniFiAlarmClient(http_session, "127.0.0.1:9", USERNAME, PASSWORD, scheme="http")
    listener, recorder, task = start_listener(client)

    await wait_until(lambda: listener.reconnect_count >= 2)

    assert not task.done()
    assert recorder.connection == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_websocket.py -v`
Expected: collection error `No module named 'custom_components.unifi_protect_alarm_bridge.websocket'`.

- [ ] **Step 3: Implement `websocket.py`**

`custom_components/unifi_protect_alarm_bridge/websocket.py`:
```python
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
            except (AuthFailed, MfaRequired):
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
        async with ws:
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
        if action.get("modelKey") != PROFILE_MODEL_KEY or action.get("action") != "update":
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_websocket.py -v`
Expected: all PASS.

- [ ] **Step 5: Run everything, lint, and commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check --fix . && .venv/bin/ruff format .
git add custom_components tests
git commit -m "feat: add Protect update websocket listener

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Config flow — user, profiles, reauth and reconfigure

**Files:**
- Modify (replace): `custom_components/unifi_protect_alarm_bridge/config_flow.py`
- Delete: `tests/test_smoke.py`, which is superseded
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes (from Task 2):
  - `UniFiAlarmClient`, used via `async_get_console_info()` and `async_get_profiles()`
  - `ArmProfile`, `ConsoleInfo`, and the error classes
- Consumes (from Task 1): the `const.py` keys.
- Produces (Tasks 6 and 8 rely on these):
  - `normalize_host(raw: str) -> str`
  - `profiles_schema(profiles: Sequence[ArmProfile], current: Mapping[str, Any]) -> vol.Schema`
  - `validate_mapping(user_input: Mapping[str, Any]) -> dict[str, str]` returns the errors dict, which is empty when valid.
  - `mapping_from_input(user_input: Mapping[str, Any]) -> dict[str, str]`
  - `CannotValidate(Exception)`, which has `.error: str`
  - `async_validate_connection(hass, host, username, password, verify_ssl) -> tuple[ConsoleInfo, list[ArmProfile]]`
  - `UniFiAlarmConfigFlow`
- The options flow is added in Task 6.

- [ ] **Step 1: Write the failing tests**

`tests/test_config_flow.py`:
```python
"""Tests for the config flow."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.unifi_protect_alarm_bridge.api import (
    AuthFailed,
    CannotConnect,
    InsufficientPermissions,
    MfaRequired,
    RateLimited,
    UnexpectedResponse,
)
from custom_components.unifi_protect_alarm_bridge.config_flow import normalize_host
from custom_components.unifi_protect_alarm_bridge.const import (
    CONF_PROFILE_AWAY,
    CONF_PROFILE_HOME,
    DOMAIN,
)

from .helpers import (
    AWAY_ID,
    CONSOLE,
    ENTRY_DATA,
    HOME_ID,
    HOST,
    PASSWORD,
    UNIQUE_ID,
    USERNAME,
    make_profile,
    mock_config_entry,
)

USER_INPUT = {
    CONF_HOST: HOST,
    CONF_USERNAME: USERNAME,
    CONF_PASSWORD: PASSWORD,
    CONF_VERIFY_SSL: False,
}


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    with patch(
        "custom_components.unifi_protect_alarm_bridge.async_setup_entry",
        return_value=True,
    ) as mock:
        yield mock


@pytest.fixture
def flow_client() -> Generator[MagicMock]:
    with patch(
        "custom_components.unifi_protect_alarm_bridge.config_flow.UniFiAlarmClient",
        autospec=True,
    ) as client_cls:
        client = client_cls.return_value
        client.async_get_console_info.return_value = CONSOLE
        client.async_get_profiles.return_value = [make_profile()]
        yield client


async def _start_user_flow(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.0.2.1", "192.0.2.1"),
        ("  192.0.2.1  ", "192.0.2.1"),
        ("https://192.0.2.1/", "192.0.2.1"),
        ("https://192.0.2.1:8443/protect/dashboard", "192.0.2.1:8443"),
        ("console.local", "console.local"),
        ("fe80::1", "[fe80::1]"),
        ("[fe80::1]:443", "[fe80::1]:443"),
    ],
)
def test_normalize_host(raw: str, expected: str) -> None:
    assert normalize_host(raw) == expected


async def test_single_profile_creates_entry(hass, flow_client) -> None:
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test Console"
    assert result["data"] == ENTRY_DATA
    assert result["options"] == {CONF_PROFILE_AWAY: AWAY_ID}
    assert result["result"].unique_id == UNIQUE_ID


async def test_url_style_host_is_normalised(hass, flow_client) -> None:
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "https://192.0.2.1/"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.0.2.1"


async def test_multiple_profiles_ask_for_mapping(hass, flow_client) -> None:
    flow_client.async_get_profiles.return_value = [
        make_profile(),
        make_profile(profile_id=HOME_ID),
    ]
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "profiles"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: AWAY_ID}
    )
    assert result["errors"] == {"base": "duplicate_profile"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: HOME_ID}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: HOME_ID}


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (AuthFailed("x"), "invalid_auth"),
        (MfaRequired("x"), "mfa_not_supported"),
        (RateLimited("x"), "rate_limited"),
        (CannotConnect("x"), "cannot_connect"),
        (InsufficientPermissions("x"), "not_super_admin"),
        (UnexpectedResponse("x"), "unknown"),
        (RuntimeError("x"), "unknown"),
    ],
)
async def test_user_step_errors_then_recovers(hass, flow_client, error, key) -> None:
    flow_client.async_get_profiles.side_effect = error
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}

    flow_client.async_get_profiles.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_failed_attempt_does_not_echo_the_password(hass, flow_client) -> None:
    flow_client.async_get_profiles.side_effect = AuthFailed("x")
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    suggested = {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    }
    assert suggested[CONF_HOST] == HOST
    assert suggested[CONF_PASSWORD] is None


async def test_global_mode_off(hass, flow_client) -> None:
    flow_client.async_get_console_info.return_value = replace(
        CONSOLE, external_alarm_manager=False
    )
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "global_mode_off"}
    flow_client.async_get_profiles.assert_not_called()


async def test_no_profiles(hass, flow_client) -> None:
    flow_client.async_get_profiles.return_value = []
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "no_profiles"}


async def test_already_configured_updates_host(hass, flow_client) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "192.0.2.99"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.0.2.99"


async def test_reauth(hass, flow_client) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    flow_client.async_get_profiles.side_effect = AuthFailed("x")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    flow_client.async_get_profiles.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"


async def test_reauth_against_a_different_console_aborts(hass, flow_client) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    flow_client.async_get_console_info.return_value = replace(CONSOLE, mac="112233445566")
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_PASSWORD] == PASSWORD


async def test_reconfigure(hass, flow_client) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "fe80::1", CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "[fe80::1]"
    assert entry.data[CONF_VERIFY_SSL] is True


async def test_reconfigure_to_a_different_console_aborts(hass, flow_client) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    flow_client.async_get_console_info.return_value = replace(CONSOLE, mac="112233445566")
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.50", CONF_VERIFY_SSL: False}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_HOST] == HOST
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `git rm -q tests/test_smoke.py && .venv/bin/pytest tests/test_config_flow.py -v`
Expected: collection error `cannot import name 'normalize_host'`.

- [ ] **Step 3: Implement the config flow**

`custom_components/unifi_protect_alarm_bridge/config_flow.py`:
```python
"""Config flow for UniFi Protect Alarm Bridge."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import aiohttp
import voluptuous as vol

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

from .api import (
    ArmProfile,
    AuthFailed,
    CannotConnect,
    ConsoleInfo,
    InsufficientPermissions,
    MfaRequired,
    RateLimited,
    UniFiAlarmClient,
    UnexpectedResponse,
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
        return self.async_create_entry(title=self._title, data=self._data, options=options)

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
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config_flow.py -v`
Expected: all PASS.

- [ ] **Step 5: Run everything, lint, and commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check --fix . && .venv/bin/ruff format .
git add -A custom_components tests
git commit -m "feat: add config flow with Super Admin validation, reauth and reconfigure

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Coordinator — push, polling, stale-write guard, promotion refresh and repairs

**Files:**
- Create: `custom_components/unifi_protect_alarm_bridge/coordinator.py`
- Test: `tests/test_coordinator.py`

**Interfaces:**
- Consumes:
  - From Task 2: `UniFiAlarmClient`, `ArmProfile`, `ConsoleInfo` and the error classes.
  - From Task 3: `ProtectUpdatesListener`.
  - From Task 1: the `const.py` keys.
  - From Task 4: the config flow's reauth step, which reauth tests start.
- Produces:
  - `type UniFiAlarmConfigEntry = ConfigEntry[UniFiAlarmCoordinator]`
  - `UniFiAlarmCoordinator(hass, entry, client, console)`, subclassing `DataUpdateCoordinator[dict[str, ArmProfile]]`:
    - Attributes: `client`, `console`, `listener`, `last_poll_success_at: datetime | None`, `last_unexpected_payload: Any`.
    - Callbacks: `async_start_push()`, `handle_profile(profile)`, `handle_connection_change(connected)`, `handle_resync()`, `handle_auth_failed()`.
    - Actions: `async async_arm_profile(profile_id)` and `async async_disarm_profile(profile_id)`. Both raise `HomeAssistantError` with translation key `arm_failed` or `disarm_failed` and placeholders `profile` and `error`.
    - `async_shutdown()`
  - `async_delete_entry_issues(hass, entry_id) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/test_coordinator.py`:
```python
"""Tests for the arm profile coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock, create_autospec

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

# Importing config_flow registers the reauth handler, as HA's integration preload
# does at runtime. Without it, a poll's ConfigEntryAuthFailed can't start reauth
# (async_start_reauth_if_available) when this file runs on its own.
from custom_components.unifi_protect_alarm_bridge import config_flow  # noqa: F401
from custom_components.unifi_protect_alarm_bridge.api import (
    AuthFailed,
    CannotConnect,
    InsufficientPermissions,
    MfaRequired,
    UnexpectedResponse,
    UniFiAlarmClient,
)
from custom_components.unifi_protect_alarm_bridge.const import (
    DOMAIN,
    ISSUE_API_CHANGED,
    ISSUE_GLOBAL_MODE_OFF,
    ISSUE_NOT_SUPER_ADMIN,
    ISSUE_PROFILE_MISSING,
    ISSUE_PUSH_UNAVAILABLE,
    POLL_INTERVAL_PUSH_DOWN,
    POLL_INTERVAL_PUSH_HEALTHY,
    PROMOTION_MAX_OVERDUE,
)
from custom_components.unifi_protect_alarm_bridge.coordinator import (
    UniFiAlarmCoordinator,
    async_delete_entry_issues,
)

from .helpers import AWAY_ID, CONSOLE, HOME_ID, make_profile, mock_config_entry


@pytest.fixture
def client() -> MagicMock:
    client = create_autospec(UniFiAlarmClient, instance=True)
    client.async_get_profiles.return_value = [make_profile()]
    client.async_get_console_info.return_value = CONSOLE
    return client


@pytest.fixture
async def coordinator(
    hass: HomeAssistant, client: MagicMock
) -> AsyncGenerator[UniFiAlarmCoordinator]:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    coordinator = UniFiAlarmCoordinator(hass, entry, client, CONSOLE)
    yield coordinator
    await coordinator.async_shutdown()


def _issue(hass: HomeAssistant, coordinator: UniFiAlarmCoordinator, key: str):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{key}_{coordinator.config_entry.entry_id}"
    )


async def test_poll_populates_data(coordinator) -> None:
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data == {AWAY_ID: make_profile()}
    assert coordinator.update_interval == POLL_INTERVAL_PUSH_DOWN
    assert coordinator.last_poll_success_at is not None


async def test_poll_started_before_a_push_does_not_overwrite_it(
    hass, coordinator, client
) -> None:
    await coordinator.async_refresh()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_poll():
        started.set()
        await release.wait()
        return [make_profile(state="disarmed")]

    client.async_get_profiles.side_effect = slow_poll
    task = hass.async_create_task(coordinator.async_refresh())
    await started.wait()

    coordinator.handle_profile(make_profile(state="arming"))
    release.set()
    await task

    assert coordinator.data[AWAY_ID].state == "arming"


async def test_poll_started_after_a_push_wins(coordinator, client) -> None:
    await coordinator.async_refresh()
    coordinator.handle_profile(make_profile(state="arming"))

    client.async_get_profiles.return_value = [make_profile(state="armed")]
    await coordinator.async_refresh()

    assert coordinator.data[AWAY_ID].state == "armed"


async def test_poll_applies_new_and_deleted_profiles(coordinator, client) -> None:
    await coordinator.async_refresh()
    coordinator.handle_profile(make_profile(state="arming"))

    client.async_get_profiles.return_value = [make_profile(profile_id=HOME_ID)]
    await coordinator.async_refresh()

    assert set(coordinator.data) == {HOME_ID}


async def test_connection_change_switches_interval_and_resyncs(
    hass, coordinator, client
) -> None:
    await coordinator.async_refresh()

    coordinator.handle_connection_change(True)
    await hass.async_block_till_done()
    assert coordinator.update_interval == POLL_INTERVAL_PUSH_HEALTHY
    assert client.async_get_profiles.await_count == 2

    coordinator.handle_connection_change(False)
    await hass.async_block_till_done()
    assert coordinator.update_interval == POLL_INTERVAL_PUSH_DOWN


@pytest.mark.parametrize("error", [AuthFailed("x"), MfaRequired("x")])
async def test_auth_failure_starts_reauth(hass, coordinator, client, error) -> None:
    client.async_get_profiles.side_effect = error
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_listener_auth_failure_starts_reauth(hass, coordinator) -> None:
    coordinator.handle_auth_failed()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_not_super_admin_issue_lifecycle(hass, coordinator, client) -> None:
    client.async_get_profiles.side_effect = InsufficientPermissions("x")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert _issue(hass, coordinator, ISSUE_NOT_SUPER_ADMIN) is not None

    client.async_get_profiles.side_effect = None
    await coordinator.async_refresh()
    assert _issue(hass, coordinator, ISSUE_NOT_SUPER_ADMIN) is None


async def test_unexpected_response_raises_api_changed(hass, coordinator, client) -> None:
    client.async_get_profiles.side_effect = UnexpectedResponse("x", {"weird": 1})
    await coordinator.async_refresh()

    issue = _issue(hass, coordinator, ISSUE_API_CHANGED)
    assert issue is not None
    assert issue.translation_placeholders["protect_version"] == "7.2.105"
    assert coordinator.last_unexpected_payload == {"weird": 1}
    assert _issue(hass, coordinator, ISSUE_GLOBAL_MODE_OFF) is None

    client.async_get_profiles.side_effect = None
    await coordinator.async_refresh()
    assert _issue(hass, coordinator, ISSUE_API_CHANGED) is None


async def test_unexpected_response_in_local_mode_raises_global_mode_off(
    hass, coordinator, client
) -> None:
    client.async_get_profiles.side_effect = UnexpectedResponse("x")
    client.async_get_console_info.return_value = replace(
        CONSOLE, external_alarm_manager=False
    )
    await coordinator.async_refresh()

    assert _issue(hass, coordinator, ISSUE_GLOBAL_MODE_OFF) is not None
    assert _issue(hass, coordinator, ISSUE_API_CHANGED) is None


async def test_empty_profiles_in_local_mode_fails(hass, coordinator, client) -> None:
    client.async_get_profiles.return_value = []
    client.async_get_console_info.return_value = replace(
        CONSOLE, external_alarm_manager=False
    )
    await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert _issue(hass, coordinator, ISSUE_GLOBAL_MODE_OFF) is not None


async def test_cannot_connect_marks_update_failed(coordinator, client) -> None:
    client.async_get_profiles.side_effect = CannotConnect("x")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success


async def test_profile_missing_issue_lifecycle(hass, coordinator, client) -> None:
    client.async_get_profiles.return_value = [make_profile(profile_id=HOME_ID)]
    await coordinator.async_refresh()
    issue = _issue(hass, coordinator, ISSUE_PROFILE_MISSING)
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": coordinator.config_entry.entry_id}

    client.async_get_profiles.return_value = [make_profile()]
    await coordinator.async_refresh()
    assert _issue(hass, coordinator, ISSUE_PROFILE_MISSING) is None


async def test_push_unavailable_issue_lifecycle(hass, coordinator, freezer) -> None:
    await coordinator.async_refresh()
    assert _issue(hass, coordinator, ISSUE_PUSH_UNAVAILABLE) is None

    freezer.tick(timedelta(hours=1, seconds=1))
    await coordinator.async_refresh()
    assert _issue(hass, coordinator, ISSUE_PUSH_UNAVAILABLE) is not None

    coordinator.handle_connection_change(True)
    await hass.async_block_till_done()
    assert _issue(hass, coordinator, ISSUE_PUSH_UNAVAILABLE) is None


async def test_promotion_refresh_fires_when_the_exit_delay_ends(
    hass, coordinator, client
) -> None:
    due = dt_util.utcnow() + timedelta(seconds=60)
    client.async_get_profiles.return_value = [
        make_profile(state="arming", promotion_due=due)
    ]
    await coordinator.async_refresh()

    client.async_get_profiles.return_value = [make_profile(state="armed")]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=63))
    await hass.async_block_till_done()

    assert coordinator.data[AWAY_ID].state == "armed"
    assert client.async_get_profiles.await_count == 2


async def test_promotion_refresh_gives_up_if_the_console_never_promotes(
    hass, coordinator, client
) -> None:
    """Clock skew: the due time is already past and the state stays 'arming'."""
    past = dt_util.utcnow() - timedelta(seconds=30)
    client.async_get_profiles.return_value = [
        make_profile(state="arming", promotion_due=past)
    ]
    await coordinator.async_refresh()

    for step in range(1, 7):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=15 * step))
        await hass.async_block_till_done()

    assert client.async_get_profiles.await_count == 1 + PROMOTION_MAX_OVERDUE


async def test_no_promotion_timer_when_arming_is_instant(coordinator, client) -> None:
    """Activation delay off: arm returns 'armed' with no due time."""
    await coordinator.async_refresh()
    client.async_arm.return_value = make_profile(state="armed", activation_delay=None)

    await coordinator.async_arm_profile(AWAY_ID)

    assert coordinator.data[AWAY_ID].state == "armed"
    assert coordinator._promotion_unsub is None


async def test_arm_applies_the_response_without_polling(coordinator, client) -> None:
    await coordinator.async_refresh()
    client.async_arm.return_value = make_profile(state="arming")

    await coordinator.async_arm_profile(AWAY_ID)

    assert coordinator.data[AWAY_ID].state == "arming"
    assert client.async_get_profiles.await_count == 1


async def test_action_errors_are_translated(coordinator, client) -> None:
    await coordinator.async_refresh()
    client.async_arm.side_effect = CannotConnect("boom")

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_arm_profile(AWAY_ID)

    assert err.value.translation_key == "arm_failed"
    assert err.value.translation_placeholders == {"profile": "Away", "error": "boom"}


async def test_action_auth_failure_starts_reauth(hass, coordinator, client) -> None:
    await coordinator.async_refresh()
    client.async_disarm.side_effect = AuthFailed("x")

    with pytest.raises(HomeAssistantError):
        await coordinator.async_disarm_profile(AWAY_ID)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_shutdown_cancels_the_promotion_timer(coordinator, client) -> None:
    due = dt_util.utcnow() + timedelta(seconds=60)
    client.async_get_profiles.return_value = [
        make_profile(state="arming", promotion_due=due)
    ]
    await coordinator.async_refresh()
    assert coordinator._promotion_unsub is not None

    await coordinator.async_shutdown()

    assert coordinator._promotion_unsub is None


async def test_push_after_shutdown_is_ignored(coordinator) -> None:
    await coordinator.async_refresh()
    await coordinator.async_shutdown()

    due = dt_util.utcnow() + timedelta(seconds=60)
    coordinator.handle_profile(make_profile(state="arming", promotion_due=due))

    assert coordinator.data[AWAY_ID].state == "disarmed"
    assert coordinator._promotion_unsub is None


async def test_delete_entry_issues(hass, coordinator, client) -> None:
    client.async_get_profiles.side_effect = InsufficientPermissions("x")
    await coordinator.async_refresh()

    async_delete_entry_issues(hass, coordinator.config_entry.entry_id)

    assert _issue(hass, coordinator, ISSUE_NOT_SUPER_ADMIN) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_coordinator.py -v`
Expected: collection error `No module named 'custom_components.unifi_protect_alarm_bridge.coordinator'`.

- [ ] **Step 3: Implement the coordinator**

`custom_components/unifi_protect_alarm_bridge/coordinator.py`:
```python
"""Coordinator merging websocket pushes, action responses and polls."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
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
    UniFiAlarmClient,
    UniFiAlarmError,
    UnexpectedResponse,
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
            if isinstance(err, AuthFailed):
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_coordinator.py -v`
Expected: all PASS.

If `test_promotion_refresh_gives_up_if_the_console_never_promotes` counts one poll too many or too few, print `client.async_get_profiles.await_count` after each loop step. Check against the rule: each firing while overdue increments the count once, and scheduling stops at `PROMOTION_MAX_OVERDUE`. Fix the code, not the assertion.

- [ ] **Step 5: Run everything, lint, and commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check --fix . && .venv/bin/ruff format .
git add custom_components tests
git commit -m "feat: add coordinator with push/poll merge, promotion refresh and repairs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Integration setup, unload, remove, and the options flow

**Files:**
- Modify (replace): `custom_components/unifi_protect_alarm_bridge/__init__.py`
- Modify: `custom_components/unifi_protect_alarm_bridge/config_flow.py`, adding the options flow
- Create: `custom_components/unifi_protect_alarm_bridge/alarm_control_panel.py` as a minimal placeholder platform so setup can forward to it (replaced in Task 7)
- Modify: `tests/conftest.py`
- Test: `tests/test_init.py`, `tests/test_options_flow.py`

**Interfaces:**
- Consumes:
  - From Task 5: `UniFiAlarmCoordinator`, `UniFiAlarmConfigEntry`, `async_delete_entry_issues`.
  - From Task 4: `profiles_schema`, `validate_mapping`, `mapping_from_input`.
  - From Task 2: `UniFiAlarmClient`.
- Produces:
  - `async_setup_entry`, `async_unload_entry` and `async_remove_entry`. After setup, `entry.runtime_data` is the coordinator.
  - `UniFiAlarmOptionsFlow`.
  - Test fixtures `mock_listener_run` and `mock_client`, and the helper `setup_entry(hass, entry)`, all in `tests/conftest.py`.

- [ ] **Step 1: Add the setup fixtures**

Replace `tests/conftest.py` with the full file below. It keeps every Task 2 fixture and adds the setup fixtures.
```python
"""Shared fixtures."""

from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_protect_alarm_bridge.api import UniFiAlarmClient

from .fake_console import FakeConsole
from .helpers import CONSOLE, PASSWORD, USERNAME, make_profile, titled_profile


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load integrations from custom_components/."""


@pytest.fixture
async def fake_console(socket_enabled) -> AsyncGenerator[FakeConsole]:
    """A running fake console on 127.0.0.1 (sockets are otherwise blocked)."""
    console = FakeConsole()
    await console.start()
    yield console
    await console.stop()


@pytest.fixture
async def http_session() -> AsyncGenerator[aiohttp.ClientSession]:
    """A session configured exactly like the integration's (unsafe cookie jar)."""
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    yield session
    await session.close()


@pytest.fixture
def api_client(
    fake_console: FakeConsole, http_session: aiohttp.ClientSession
) -> UniFiAlarmClient:
    """A real client pointed at the fake console over plain HTTP."""
    return UniFiAlarmClient(
        http_session, fake_console.host, USERNAME, PASSWORD, scheme="http"
    )


@pytest.fixture
def mock_listener_run() -> Generator[AsyncMock]:
    """Don't open a real websocket during integration tests."""
    with patch(
        "custom_components.unifi_protect_alarm_bridge.websocket.ProtectUpdatesListener.run",
        new_callable=AsyncMock,
    ) as run:
        yield run


@pytest.fixture
def mock_client(mock_listener_run: AsyncMock) -> Generator[MagicMock]:
    """Replace the API client used by async_setup_entry."""
    with patch(
        "custom_components.unifi_protect_alarm_bridge.UniFiAlarmClient", autospec=True
    ) as client_cls:
        client = client_cls.return_value
        client.async_get_console_info.return_value = CONSOLE
        client.async_get_profiles.return_value = [make_profile()]
        client.async_arm.side_effect = lambda pid: titled_profile(pid, "arming")
        client.async_disarm.side_effect = lambda pid: titled_profile(pid, "disarmed")
        yield client


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up an entry, and wait for it to settle."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
```

- [ ] **Step 2: Write the failing tests**

`tests/test_init.py`:
```python
"""Tests for setting up, unloading and removing the integration."""

import asyncio
from datetime import timedelta

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.unifi_protect_alarm_bridge.api import (
    AuthFailed,
    CannotConnect,
    MfaRequired,
    RateLimited,
    UnexpectedResponse,
)
from custom_components.unifi_protect_alarm_bridge.const import (
    DOMAIN,
    ISSUE_API_CHANGED,
)

from .conftest import setup_entry
from .helpers import ENTITY_ID, HOME_ID, make_profile, mock_config_entry


async def test_setup_unload(hass, mock_client, mock_listener_run) -> None:
    entry = mock_config_entry()
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data is not None
    mock_listener_run.assert_awaited_once()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_unload_cancels_socket_task_and_timers(
    hass, mock_client, mock_listener_run
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def run_forever() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    mock_listener_run.side_effect = run_forever
    due = dt_util.utcnow() + timedelta(seconds=60)
    mock_client.async_get_profiles.return_value = [
        make_profile(state="arming", promotion_due=due)
    ]
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    await started.wait()
    coordinator = entry.runtime_data
    assert coordinator._promotion_unsub is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert cancelled.is_set()
    assert coordinator._promotion_unsub is None


@pytest.mark.parametrize(
    ("state", "expected"),
    [("armed", "armed_away"), ("breached", "triggered"), ("arming", "arming")],
)
async def test_restart_while_active_shows_the_real_state(
    hass, mock_client, state, expected
) -> None:
    mock_client.async_get_profiles.return_value = [make_profile(state=state)]
    await setup_entry(hass, mock_config_entry())
    assert hass.states.get(ENTITY_ID).state == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CannotConnect("x"), ConfigEntryState.SETUP_RETRY),
        (RateLimited("x"), ConfigEntryState.SETUP_RETRY),
        (UnexpectedResponse("x"), ConfigEntryState.SETUP_RETRY),
        (AuthFailed("x"), ConfigEntryState.SETUP_ERROR),
        (MfaRequired("x"), ConfigEntryState.SETUP_ERROR),
    ],
)
async def test_console_info_errors(hass, mock_client, error, expected) -> None:
    mock_client.async_get_console_info.side_effect = error
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    assert entry.state is expected


async def test_auth_failure_during_setup_starts_reauth(hass, mock_client) -> None:
    mock_client.async_get_console_info.side_effect = AuthFailed("x")
    await setup_entry(hass, mock_config_entry())
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_first_refresh_failure_retries(hass, mock_client) -> None:
    mock_client.async_get_profiles.side_effect = CannotConnect("x")
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_missing_profile_still_loads(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [make_profile(profile_id=HOME_ID)]
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED


async def test_remove_entry_deletes_issues(hass, mock_client) -> None:
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    issue_id = f"{ISSUE_API_CHANGED}_{entry.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_API_CHANGED,
        translation_placeholders={"protect_version": "x", "issue_url": "x"},
    )

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
```

`tests/test_options_flow.py`:
```python
"""Tests for the options flow (arm mode mapping)."""

from homeassistant.components.alarm_control_panel import AlarmControlPanelEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.data_entry_flow import FlowResultType

from custom_components.unifi_protect_alarm_bridge.const import (
    CONF_PROFILE_AWAY,
    CONF_PROFILE_HOME,
)

from .conftest import setup_entry
from .helpers import AWAY_ID, ENTITY_ID, HOME_ID, make_profile, mock_config_entry


async def test_options_flow_updates_mapping_and_reloads(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(),
        make_profile(profile_id=HOME_ID),
    ]
    entry = mock_config_entry()
    await setup_entry(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: AWAY_ID}
    )
    assert result["errors"] == {"base": "duplicate_profile"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: HOME_ID}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options == {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: HOME_ID}
    features = hass.states.get(ENTITY_ID).attributes[ATTR_SUPPORTED_FEATURES]
    assert features & AlarmControlPanelEntityFeature.ARM_HOME


async def test_options_flow_needs_a_loaded_entry(hass) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_init.py tests/test_options_flow.py -v`
Expected: FAIL. `mock_client` can't patch `custom_components.unifi_protect_alarm_bridge.UniFiAlarmClient` (the attribute doesn't exist), and the options flow isn't supported.

- [ ] **Step 4: Implement setup and the options flow**

`custom_components/unifi_protect_alarm_bridge/__init__.py`:
```python
"""UniFi Protect Alarm Bridge integration."""

from __future__ import annotations

import aiohttp

from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import AuthFailed, MfaRequired, UniFiAlarmClient, UniFiAlarmError
from .coordinator import (
    UniFiAlarmConfigEntry,
    UniFiAlarmCoordinator,
    async_delete_entry_issues,
)

PLATFORMS = [Platform.ALARM_CONTROL_PANEL]


async def async_setup_entry(hass: HomeAssistant, entry: UniFiAlarmConfigEntry) -> bool:
    """Set up the console from a config entry."""
    # Created inside setup, so HA closes it when the entry unloads (auto_cleanup).
    session = async_create_clientsession(
        hass,
        verify_ssl=entry.data[CONF_VERIFY_SSL],
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    )
    client = UniFiAlarmClient(
        session, entry.data[CONF_HOST], entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
    )
    try:
        console = await client.async_get_console_info()
    except (AuthFailed, MfaRequired) as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except UniFiAlarmError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = UniFiAlarmCoordinator(hass, entry, client, console)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.async_start_push()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UniFiAlarmConfigEntry) -> bool:
    """Unload a config entry (the socket task and session close automatically)."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: UniFiAlarmConfigEntry) -> None:
    """Delete this entry's repair issues."""
    async_delete_entry_issues(hass, entry.entry_id)
```

Placeholder `custom_components/unifi_protect_alarm_bridge/alarm_control_panel.py` (Task 7 replaces it):
```python
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
```

In `config_flow.py`, make these changes:

1. Extend the config-entries import:
   ```python
   from homeassistant.config_entries import (
       ConfigEntry,
       ConfigEntryState,
       ConfigFlow,
       ConfigFlowResult,
       OptionsFlowWithReload,
   )
   ```
2. Add `from homeassistant.core import HomeAssistant, callback`.
3. Add this method to `UniFiAlarmConfigFlow`:
   ```python
       @staticmethod
       @callback
       def async_get_options_flow(config_entry: ConfigEntry) -> UniFiAlarmOptionsFlow:
           """Return the options flow."""
           return UniFiAlarmOptionsFlow()
   ```
4. Append this class at the end of the file:
   ```python
   class UniFiAlarmOptionsFlow(OptionsFlowWithReload):
       """Change which Protect profile each arm mode uses."""

       async def async_step_init(
           self, user_input: dict[str, Any] | None = None
       ) -> ConfigFlowResult:
           """Show the mapping form with the console's current profiles."""
           entry = self.config_entry
           if entry.state is not ConfigEntryState.LOADED:
               return self.async_abort(reason="not_loaded")
           profiles = list(entry.runtime_data.data.values())
           errors: dict[str, str] = {}
           if user_input is not None and not (errors := validate_mapping(user_input)):
               return self.async_create_entry(data=mapping_from_input(user_input))
           return self.async_show_form(
               step_id="init",
               data_schema=profiles_schema(profiles, user_input or entry.options),
               errors=errors,
           )
   ```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/test_init.py tests/test_options_flow.py -v`
Expected: PASS, except these two, which need the entity from Task 7 and fail with `AttributeError: 'NoneType' object has no attribute 'state'` / `'attributes'`:
- `test_restart_while_active_shows_the_real_state`
- `test_options_flow_updates_mapping_and_reloads`

Mark those two tests `@pytest.mark.skip(reason="entity lands in Task 7")` now. Task 7 Step 5 removes the marks.

- [ ] **Step 6: Run everything, lint, and commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check --fix . && .venv/bin/ruff format .
git add custom_components tests
git commit -m "feat: set up coordinator per entry and add options flow

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: The alarm control panel entity

**Files:**
- Modify (replace): `custom_components/unifi_protect_alarm_bridge/alarm_control_panel.py`
- Modify: `tests/test_init.py` and `tests/test_options_flow.py`, removing the Task 6 skip marks
- Test: `tests/test_alarm_control_panel.py`

**Interfaces:**
- Consumes:
  - From Task 5: `UniFiAlarmCoordinator`, via `.data`, `.console`, `.config_entry`, `async_arm_profile` and `async_disarm_profile`. The latter two raise `HomeAssistantError(translation_key in {"arm_failed","disarm_failed"})`.
  - From Task 1: the `const.py` keys.
  - From Task 6: the `mock_client` and `setup_entry` test fixtures.
- Produces: `UniFiAlarmPanel`. Its entity ID is `alarm_control_panel.test_console` in tests.

- [ ] **Step 1: Write the failing tests**

`tests/test_alarm_control_panel.py`:
```python
"""Tests for the alarm control panel entity."""

import pytest

from homeassistant.components.alarm_control_panel import (
    DOMAIN as ALARM_DOMAIN,
    AlarmControlPanelEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from custom_components.unifi_protect_alarm_bridge.api import CannotConnect
from custom_components.unifi_protect_alarm_bridge.const import (
    CONF_PROFILE_AWAY,
    CONF_PROFILE_HOME,
    DOMAIN,
)

from .conftest import setup_entry
from .helpers import (
    AWAY_ID,
    ENTITY_ID,
    HOME_ID,
    OTHER_ID,
    UNIQUE_ID,
    make_profile,
    mock_config_entry,
)

TWO_MODES = {CONF_PROFILE_AWAY: AWAY_ID, CONF_PROFILE_HOME: HOME_ID}


async def _call(hass, service: str) -> None:
    await hass.services.async_call(
        ALARM_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("disarmed", "disarmed"),
        ("arming", "arming"),
        ("armed", "armed_away"),
        ("breached", "triggered"),
        ("some_future_state", STATE_UNKNOWN),
    ],
)
async def test_state_mapping(hass, mock_client, state, expected) -> None:
    mock_client.async_get_profiles.return_value = [make_profile(state=state)]
    await setup_entry(hass, mock_config_entry())
    assert hass.states.get(ENTITY_ID).state == expected


async def test_armed_home_profile(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(),
        make_profile(profile_id=HOME_ID, state="armed"),
    ]
    await setup_entry(hass, mock_config_entry(TWO_MODES))
    assert hass.states.get(ENTITY_ID).state == "armed_home"


async def test_unmapped_profile_armed_elsewhere(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(),
        make_profile(profile_id=OTHER_ID, state="armed"),
    ]
    await setup_entry(hass, mock_config_entry())
    state = hass.states.get(ENTITY_ID)
    assert state.state == "armed_custom_bypass"
    assert state.attributes["profile_title"] == "Other"


async def test_most_severe_profile_wins(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(state="armed"),
        make_profile(profile_id=HOME_ID, state="breached"),
    ]
    await setup_entry(hass, mock_config_entry(TWO_MODES))
    state = hass.states.get(ENTITY_ID)
    assert state.state == "triggered"
    assert state.attributes["profile_title"] == "Home"


async def test_push_update_changes_state(hass, mock_client) -> None:
    entry = mock_config_entry()
    await setup_entry(hass, entry)

    entry.runtime_data.handle_profile(make_profile(state="breached"))
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == "triggered"


async def test_supported_features_follow_mapping(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(),
        make_profile(profile_id=HOME_ID),
    ]
    await setup_entry(hass, mock_config_entry(TWO_MODES))
    features = hass.states.get(ENTITY_ID).attributes[ATTR_SUPPORTED_FEATURES]
    assert features == (
        AlarmControlPanelEntityFeature.ARM_AWAY | AlarmControlPanelEntityFeature.ARM_HOME
    )


async def test_attributes_and_device(hass, mock_client) -> None:
    await setup_entry(hass, mock_config_entry())
    attributes = hass.states.get(ENTITY_ID).attributes
    assert attributes["profile_title"] == "Away"
    assert attributes["activation_delay"] == 60
    assert attributes["state_set_at"] == "2026-09-27T13:55:44+00:00"
    assert attributes["code_arm_required"] is False

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, UNIQUE_ID)})
    assert device.configuration_url == "https://192.0.2.1"
    assert device.sw_version == "7.2.105"
    assert device.model == "UDM-PRO"


async def test_arm_away_uses_the_response(hass, mock_client) -> None:
    await setup_entry(hass, mock_config_entry())
    polls = mock_client.async_get_profiles.await_count

    await _call(hass, "alarm_arm_away")

    mock_client.async_arm.assert_awaited_once_with(AWAY_ID)
    assert hass.states.get(ENTITY_ID).state == "arming"
    assert mock_client.async_get_profiles.await_count == polls


async def test_arm_with_no_exit_delay_is_immediately_armed(hass, mock_client) -> None:
    mock_client.async_arm.side_effect = lambda pid: make_profile(
        profile_id=pid, state="armed", activation_delay=None
    )
    await setup_entry(hass, mock_config_entry())

    await _call(hass, "alarm_arm_away")

    assert hass.states.get(ENTITY_ID).state == "armed_away"


@pytest.mark.parametrize("state", ["arming", "armed"])
async def test_arm_again_while_active_is_a_no_op(hass, mock_client, state) -> None:
    mock_client.async_get_profiles.return_value = [make_profile(state=state)]
    await setup_entry(hass, mock_config_entry())
    assert hass.states.get(ENTITY_ID) is not None  # else the call is a silent no-op

    await _call(hass, "alarm_arm_away")

    mock_client.async_arm.assert_not_awaited()


async def test_arm_disarms_other_active_profiles_first(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(),
        make_profile(profile_id=HOME_ID, state="armed"),
    ]
    await setup_entry(hass, mock_config_entry(TWO_MODES))

    await _call(hass, "alarm_arm_away")

    mock_client.async_disarm.assert_awaited_once_with(HOME_ID)
    mock_client.async_arm.assert_awaited_once_with(AWAY_ID)
    assert hass.states.get(ENTITY_ID).state == "arming"


async def test_arm_missing_profile_raises(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [make_profile(profile_id=HOME_ID)]
    await setup_entry(hass, mock_config_entry())

    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "alarm_arm_away")

    assert err.value.translation_key == "profile_missing"


async def test_arm_failure_raises(hass, mock_client) -> None:
    mock_client.async_arm.side_effect = CannotConnect("boom")
    await setup_entry(hass, mock_config_entry())

    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "alarm_arm_away")

    assert err.value.translation_key == "arm_failed"


async def test_disarm_when_already_disarmed_is_a_no_op(hass, mock_client) -> None:
    await setup_entry(hass, mock_config_entry())
    assert hass.states.get(ENTITY_ID).state == "disarmed"

    await _call(hass, "alarm_disarm")

    mock_client.async_disarm.assert_not_awaited()


async def test_disarm_all_reports_partial_failure(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(state="armed"),
        make_profile(profile_id=HOME_ID, state="armed"),
    ]

    def disarm(profile_id: str):
        if profile_id == AWAY_ID:
            raise CannotConnect("boom")
        return make_profile(profile_id=profile_id, state="disarmed")

    mock_client.async_disarm.side_effect = disarm
    entry = mock_config_entry(TWO_MODES)
    await setup_entry(hass, entry)

    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, "alarm_disarm")

    assert err.value.translation_key == "disarm_failed"
    assert err.value.translation_placeholders["profile"] == "Away"
    assert mock_client.async_disarm.await_count == 2
    assert entry.runtime_data.data[HOME_ID].state == "disarmed"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_alarm_control_panel.py -v`
Expected: FAIL, because the placeholder platform adds no entity (`AttributeError: 'NoneType' object has no attribute 'state'`).

- [ ] **Step 3: Implement the entity**

`custom_components/unifi_protect_alarm_bridge/alarm_control_panel.py`:
```python
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


class UniFiAlarmPanel(CoordinatorEntity[UniFiAlarmCoordinator], AlarmControlPanelEntity):
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
            return MODE_STATES[mode] if mode else AlarmControlPanelState.ARMED_CUSTOM_BYPASS
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
        if target.state != STATE_DISARMED:
            return  # already arming/armed/breached: a second press does nothing
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
                    "error": "; ".join(str(err.__cause__ or err) for _, err in failures),
                },
            )
```

- [ ] **Step 4: Run the entity tests to verify they pass**

Run: `.venv/bin/pytest tests/test_alarm_control_panel.py -v`
Expected: all PASS.

- [ ] **Step 5: Re-enable the Task 6 tests that needed the entity**

Delete the two `@pytest.mark.skip(reason="entity lands in Task 7")` lines from `tests/test_init.py` and `tests/test_options_flow.py`.
Run: `.venv/bin/pytest tests/test_init.py tests/test_options_flow.py -v`
Expected: all PASS.

- [ ] **Step 6: Run everything, lint, and commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check --fix . && .venv/bin/ruff format .
git add custom_components tests
git commit -m "feat: add alarm control panel entity

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Diagnostics and the `profile_missing` repair flow

**Files:**
- Create: `custom_components/unifi_protect_alarm_bridge/diagnostics.py`, `custom_components/unifi_protect_alarm_bridge/repairs.py`
- Test: `tests/test_diagnostics.py`, `tests/test_repairs.py`

**Interfaces:**
- Consumes:
  - From Task 5: the coordinator attributes `listener`, `console`, `last_poll_success_at`, `last_unexpected_payload` and `update_interval`.
  - From Task 2: `ArmProfile.as_dict()`.
  - From Task 4: `profiles_schema`, `validate_mapping`, `mapping_from_input`.
  - From Task 1: `REDACT_KEYS` and `ISSUE_PROFILE_MISSING`.
- Produces:
  - `async_get_config_entry_diagnostics(hass, entry) -> dict[str, Any]`
  - `async_create_fix_flow(hass, issue_id, data) -> RepairsFlow`
  - `ProfileMissingRepairFlow`

- [ ] **Step 1: Write the failing tests**

`tests/test_diagnostics.py`:
```python
"""Tests for diagnostics redaction."""

import json

from custom_components.unifi_protect_alarm_bridge.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import setup_entry
from .helpers import AWAY_ID, HOST, PASSWORD, USERNAME, mock_config_entry


async def test_diagnostics_are_redacted(hass, mock_client) -> None:
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    entry.runtime_data.last_unexpected_payload = [
        {"id": AWAY_ID, "title": "Jane's house", "state": "odd"}
    ]

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    dumped = json.dumps(diagnostics)
    for secret in (HOST, USERNAME, PASSWORD, "Test Console", "Jane's house", '"Away"'):
        assert secret not in dumped
    assert diagnostics["profiles"][AWAY_ID]["state"] == "disarmed"
    assert diagnostics["console"]["protect_version"] == "7.2.105"
    assert diagnostics["console"]["unifi_os_version"] == "5.1.33"
    assert diagnostics["push"] == {
        "connected": False,
        "last_message_at": None,
        "reconnect_count": 0,
    }
    assert diagnostics["polling"]["last_update_success"] is True
    assert diagnostics["last_unexpected_payload"][0]["state"] == "odd"
```

`tests/test_repairs.py`:
```python
"""Tests for the profile_missing repair flow."""

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from custom_components.unifi_protect_alarm_bridge.const import (
    CONF_PROFILE_AWAY,
    CONF_PROFILE_HOME,
    DOMAIN,
    ISSUE_PROFILE_MISSING,
)
from custom_components.unifi_protect_alarm_bridge.repairs import async_create_fix_flow

from .conftest import setup_entry
from .helpers import HOME_ID, OTHER_ID, make_profile, mock_config_entry


async def _start(hass, issue_id: str, data):
    flow = await async_create_fix_flow(hass, issue_id, data)
    flow.hass = hass
    flow.flow_id = "test-flow"
    flow.handler = DOMAIN
    flow.issue_id = issue_id
    flow.data = data
    flow.context = {}
    return flow


async def test_profile_missing_fix_flow(hass, mock_client) -> None:
    mock_client.async_get_profiles.return_value = [
        make_profile(profile_id=HOME_ID),
        make_profile(profile_id=OTHER_ID),
    ]
    entry = mock_config_entry()
    await setup_entry(hass, entry)
    issue_id = f"{ISSUE_PROFILE_MISSING}_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None

    flow = await _start(hass, issue_id, issue.data)
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM

    result = await flow.async_step_init(
        {CONF_PROFILE_AWAY: HOME_ID, CONF_PROFILE_HOME: HOME_ID}
    )
    assert result["errors"] == {"base": "duplicate_profile"}

    result = await flow.async_step_init({CONF_PROFILE_AWAY: HOME_ID})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options == {CONF_PROFILE_AWAY: HOME_ID}
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_fix_flow_aborts_when_entry_not_loaded(hass) -> None:
    entry = mock_config_entry()
    entry.add_to_hass(hass)
    flow = await _start(hass, "profile_missing_x", {"entry_id": entry.entry_id})
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_diagnostics.py tests/test_repairs.py -v`
Expected: collection errors for the missing `diagnostics` / `repairs` modules.

- [ ] **Step 3: Implement diagnostics and repairs**

`custom_components/unifi_protect_alarm_bridge/diagnostics.py`:
```python
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
                "update_interval_seconds": interval.total_seconds() if interval else None,
                "last_update_success": coordinator.last_update_success,
                "last_poll_success_at": _iso(coordinator.last_poll_success_at),
            },
            "last_unexpected_payload": coordinator.last_unexpected_payload,
        },
        REDACT_KEYS,
    )
```

`custom_components/unifi_protect_alarm_bridge/repairs.py`:
```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_diagnostics.py tests/test_repairs.py -v`
Expected: all PASS.

If `test_profile_missing_fix_flow`'s last assertion fails because the issue still exists, the reload hasn't finished. Add a second `await hass.async_block_till_done()`. Don't remove the assertion: it proves the issue clears itself once the mapping is valid.

- [ ] **Step 5: Run everything, lint, and commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check --fix . && .venv/bin/ruff format .
git add custom_components tests
git commit -m "feat: add diagnostics and profile_missing repair flow

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Brand icon, release workflow, dev harness and documentation

**Files:**
- Create: `custom_components/unifi_protect_alarm_bridge/brand/icon.png`, `.../brand/icon@2x.png`
- Create: `.github/workflows/release.yml`, `scripts/develop`, `config/configuration.yaml`, `CONTRIBUTING.md`
- Modify (replace): `README.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: the whole integration.
- Produces: the release packaging that HACS consumes (a zip named per `hacs.json`), plus the docs.

- [ ] **Step 1: Generate the brand icons**

This is a one-off, and the script isn't committed. Run from the repo root:
```bash
mkdir -p custom_components/unifi_protect_alarm_bridge/brand
python3 - <<'EOF'
import math, struct, zlib

def render(size: int) -> bytes:
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            dx, dy = (x + 0.5) / size - 0.5, (y + 0.5) / size - 0.5
            keyhole = math.hypot(dx, dy + 0.08) < 0.1 or (
                -0.02 < dy < 0.22 and abs(dx) < 0.04 + (dy + 0.02) * 0.25
            )
            if keyhole:
                row += bytes((255, 255, 255, 255))
            elif math.hypot(dx, dy) < 0.46:
                row += bytes((5, 89, 201, 255))  # UniFi blue
            else:
                row += bytes((0, 0, 0, 0))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )

for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
    with open(f"custom_components/unifi_protect_alarm_bridge/brand/{name}", "wb") as f:
        f.write(render(size))
EOF
```
Verify: `file custom_components/unifi_protect_alarm_bridge/brand/*.png` reports `PNG image data, 256 x 256` and `512 x 512`, RGBA. Open `icon.png` with the Read tool to check it looks like a blue disc with a white keyhole.

- [ ] **Step 2: Write the release workflow**

`.github/workflows/release.yml`:
```yaml
name: Release

on:
  push:
    tags: ["v*"]

permissions:
  contents: write

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5

      - name: Stamp manifest version from the tag
        run: |
          python3 - "${GITHUB_REF_NAME#v}" <<'EOF'
          import json, sys
          path = "custom_components/unifi_protect_alarm_bridge/manifest.json"
          with open(path) as f:
              manifest = json.load(f)
          manifest["version"] = sys.argv[1]
          with open(path, "w") as f:
              json.dump(manifest, f, indent=2)
              f.write("\n")
          EOF

      - name: Zip the integration
        run: |
          cd custom_components/unifi_protect_alarm_bridge
          zip -r "$GITHUB_WORKSPACE/unifi_protect_alarm_bridge.zip" . -x '*__pycache__*'

      - name: Create the GitHub release
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          prerelease=""
          case "$GITHUB_REF_NAME" in *-*) prerelease="--prerelease" ;; esac
          gh release create "$GITHUB_REF_NAME" unifi_protect_alarm_bridge.zip \
            --title "$GITHUB_REF_NAME" --generate-notes $prerelease
```

- [ ] **Step 3: Write the dev harness**

`scripts/develop`:
```bash
#!/usr/bin/env bash
# Run a throwaway Home Assistant with this integration symlinked in, for testing
# against a real console. Credentials are typed into the HA UI and stay in the
# gitignored config/.storage directory.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -x .venv-dev/bin/hass ]]; then
  uv venv .venv-dev -p 3.14
  uv pip install --python .venv-dev "homeassistant==2026.9.4"
fi

mkdir -p config/custom_components
ln -sfn ../../custom_components/unifi_protect_alarm_bridge \
  config/custom_components/unifi_protect_alarm_bridge

exec .venv-dev/bin/hass --config "${PWD}/config" --debug
```
Then: `chmod +x scripts/develop`.

`config/configuration.yaml`:
```yaml
# Development-only Home Assistant config used by scripts/develop.
default_config:

logger:
  default: info
  logs:
    custom_components.unifi_protect_alarm_bridge: debug
```

- [ ] **Step 4: Write `README.md`**

```markdown
# UniFi Protect Alarm Bridge

A Home Assistant alarm panel for UniFi Protect's **Global Alarm Manager**.

Protect switches Alarm Manager to Global mode as soon as sensors, relays, fobs or
an Alarm Hub are adopted. In Global mode the public Protect API cannot arm,
disarm or read the arm state, so the official UniFi Protect integration cannot
offer an alarm panel. This integration talks to Alarm Manager directly:

- **State** comes from Alarm Manager's arm profiles, pushed instantly over
  Protect's update websocket, with polling as a safety net: every 60 s while the
  websocket works, and every 10 s while it doesn't.
- **Arm and disarm** call Alarm Manager's own profile actions.
- The panel shows `disarmed`, `arming` (during the exit delay), `armed_away`,
  `armed_home` or `armed_night`, and `triggered`. It never assumes a state it
  hasn't read from the console.

It runs alongside the official UniFi Protect integration and doesn't depend on it.

> **Unofficial.** This uses undocumented UniFi OS APIs, which a firmware update
> could change. If that happens you'll get a repair notice in Home Assistant
> rather than a silently wrong alarm state.

## Prerequisites

1. **Protect's Alarm Manager in Global mode, with at least one arm profile.**
2. **A dedicated local Super Admin account.** Create one in UniFi OS under
   **Admins & Users → Add Admin**:
   - **Restrict to local access only.** No UI.com cloud account.
   - **No two-factor authentication.** Home Assistant cannot answer an MFA
     challenge.
   - **Role: Super Admin.** Alarm Manager is a UniFi OS feature, and no custom
     role grants it. Testing showed that Protect "Full Management", Network
     "Site Admin" and user-management "View Only" are all refused.

> **Security note.** Home Assistant stores these credentials unencrypted in
> `.storage/core.config_entries`, and a Super Admin can control the whole
> console, including network, cameras and users. That's why the account should
> be dedicated to Home Assistant, local-only, and used for nothing else. Remove
> it if you stop using this integration.

## Installation (HACS)

1. In HACS, open **⋮ → Custom repositories**. Add
   `https://github.com/violuke/ha_unifi_protect_alarm_bridge` with the category
   **Integration**.
2. While only beta versions exist, open the repository in HACS, choose
   **⋮ → Redownload**, and enable **Show beta versions**.
3. Download it, then restart Home Assistant.

## Setup

1. Go to **Settings → Devices & services → Add integration → UniFi Protect
   Alarm Bridge**.
2. Enter the console's IP address or hostname, and the Super Admin account's
   username and password. Leave **Verify SSL certificate** off unless the
   console has a certificate your Home Assistant trusts.
3. If you have more than one arm profile, choose which profile **Arm away**
   uses. **Arm home** and **Arm night** are optional.

You can change the mapping later under the integration's **Configure** button.

### Behaviour worth knowing

- **Arming from HA respects the profile's exit delay.** The panel shows
  `arming` until Protect reports `armed`.
- **Arming one mode disarms any other active profile first.**
- **Disarm disarms every active profile.**
- **Arming from the Protect app, a schedule or a fob shows up within about a
  second.** If a profile you haven't mapped is armed, the panel shows
  `armed_custom_bypass`, and its `profile_title` attribute names the profile.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Entity **unavailable** | The console can't be reached, or credentials were rejected | Check **Settings → Repairs**; a "Re-authentication required" card means the password changed |
| Repair: **account is not a Super Admin** | The account's role was changed | Give it the Super Admin role again |
| Repair: **not in Global mode** | Alarm Manager switched back to Local mode (e.g. after a firmware update) | Switch it to Global in Protect |
| Repair: **instant updates unavailable** | The websocket has been down for over an hour, usually after a firmware change | State still updates every 10 s; report it if it persists |
| Repair: **mapped profile no longer exists** | An arm profile was deleted or recreated | Click **Fix** and choose the profiles again |
| **Arm does nothing** | Wrong profile mapping, or the profile itself is misconfigured in Protect | Check **Configure**, then try arming the same profile from Protect |
| Setup error **two-factor** | The account has MFA | Use a local-only account without MFA |

When reporting a problem, attach **Download diagnostics** from the integration's
menu. Hostnames, credentials, and profile and console names are redacted.

## License

MIT
```

- [ ] **Step 5: Write `CONTRIBUTING.md`**

```markdown
# Contributing

## Tests

The integration supports Home Assistant 2026.9 and later, on Python 3.14:

```bash
uv venv .venv -p 3.14 && uv pip install --python .venv -r requirements_test.txt

.venv/bin/pytest
.venv/bin/pytest tests/test_api.py::test_expired_session_logs_in_again_once -v   # one test
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

`tests/test_api.py` and `tests/test_websocket.py` run the real client against
`tests/fake_console.py`, a local aiohttp server. Test data must use placeholder
values only: see `tests/helpers.py`.

## CI

`.github/workflows/validate.yml` runs on every push and pull request:

- hassfest
- HACS validation
- ruff
- pytest

Pull requests must pass all of them.

## Trying it against a real console

```bash
scripts/develop
```

This starts Home Assistant on http://localhost:8123, with the integration
symlinked in. Add the integration through the UI. Credentials stay in the
gitignored `config/.storage`, so never put them in committed files.

## Releases

- Branch from `main` and open a pull request.
- A maintainer tags `main` as `vX.Y.Z` (or `vX.Y.Z-betaN` for a pre-release).
- `release.yml` stamps the manifest version, zips the integration, and
  publishes a GitHub release. Tags with a `-suffix` are marked as
  pre-releases, which HACS only shows to users who enable beta versions.
```

- [ ] **Step 6: Replace `CLAUDE.md`**

```markdown
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A HACS custom integration (`custom_components/unifi_protect_alarm_bridge`) that exposes UniFi Protect's **Global Alarm Manager** as a Home Assistant `alarm_control_panel`.

- The design and the verified API contract are in `docs/superpowers/specs/2026-09-27-unifi-protect-alarm-bridge-design.md`.
- The implementation plan is in `docs/superpowers/plans/`.

## Commands

```bash
uv venv .venv -p 3.14 && uv pip install --python .venv -r requirements_test.txt   # HA 2026.9 (minimum supported)
.venv/bin/pytest                       # all tests
.venv/bin/pytest tests/test_api.py::test_concurrent_expiry_logs_in_once -v   # one test
.venv/bin/ruff check --fix . && .venv/bin/ruff format .
scripts/develop                        # local HA with the integration, for real-console testing
```

## Architecture

**`api.py`** (no HA imports)
- Session-authenticated client.
- Logs in with `POST /api/auth/login` (the `TOKEN` cookie plus `X-CSRF-Token`).
- Reads state from `GET /api/v2/alarms/profiles`.
- Arms and disarms with `POST /api/v2/alarms/profiles/{id}/actions/{arm|disarm}`.
- Gets console identity and the Global-mode flag from `GET /proxy/protect/api/nvr`.
- Login is behind a lock with a generation counter.
- 401 and 403 are retried once after a fresh login before a 403 is classified as `InsufficientPermissions`.
- A wrong password returns **403** at login.

**`websocket.py`** (no HA imports)
- Decodes Protect's `ws/updates` binary frames.
- Forwards only `externalArmProfile` updates, which carry the full profile.
- Uses a heartbeat and a silence timeout, reconnects with backoff, and stops (so HA starts reauth) when a login fails.

**`coordinator.py`**
- Holds `dict[profile_id, ArmProfile]`.
- Pushes and action responses are stamped with a write sequence, so an in-flight poll can't overwrite them.
- Polls every 60 s while the socket is healthy and every 10 s while it's down.
- A one-off refresh fires at `state_promotion_due_at`, the end of the exit delay.
- Owns all repair issues, with IDs of the form `<key>_<entry_id>`.

**`alarm_control_panel.py`**
- Reports the most severe profile (`breached` > `arming` > `armed`), preferring mapped profiles on ties.
- Arm disarms other active profiles first. Disarm disarms all of them.

**Config**
- Credentials are in `entry.data`.
- The mapping of arm modes to profiles is in `entry.options`, edited through the options flow or the `profile_missing` repair flow.

## Rules

- **The API account must be Super Admin.** Custom roles get 403 from Alarm Manager.
- **Every console session uses `aiohttp.CookieJar(unsafe=True)`.** The default jar drops cookies from IP hosts.
- **Never commit real hosts, credentials, MACs or profile IDs.** Tests use the placeholders in `tests/helpers.py`.
- **Never arm or disarm a real console without the maintainer's explicit OK.**
- **Don't push tags or create releases unless asked.**
```

- [ ] **Step 7: Validate and commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check --fix . && .venv/bin/ruff format .
git add -A custom_components .github scripts config/configuration.yaml README.md CONTRIBUTING.md CLAUDE.md
git status --short   # config/ must show only configuration.yaml; no .env, no .har
git commit -m "docs: add README, CONTRIBUTING, dev harness, brand icon and release workflow

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Verify against the real console

**Files:**
- Modify: `docs/superpowers/specs/2026-09-27-unifi-protect-alarm-bridge-design.md`, specifically §3 "Not yet verified"

**Interfaces:**
- Consumes: the finished integration running via `scripts/develop`.
- Produces: spec §3 updated with what was actually observed.

This task is interactive. **Ask the maintainer before every step that arms or disarms the system.**

- [ ] **Step 1: Start the dev instance**

Run `scripts/develop` in the background and wait for `Home Assistant initialized` in its output. Ask the maintainer to open http://localhost:8123, create the local HA user, and add **UniFi Protect Alarm Bridge** with their console and Super Admin account.

Expected:
- The config flow completes.
- `alarm_control_panel.<console_name>` shows `disarmed`.
- The HA log shows no `Protect update socket disconnected` warning.

- [ ] **Step 2: Check push without arming**

Ask the maintainer to open **Settings → Devices & services → UniFi Protect Alarm Bridge → ⋮ → Download diagnostics** and share the `push` section.
Expected: `"connected": true` and a recent `last_message_at`.

- [ ] **Step 3: Arm/disarm round trip (ask first)**

Ask: "May I arm `<profile>` from Home Assistant, watch it go arming → armed, then disarm? The exit delay is `<activation_delay>` s, notifications will fire, and a breach during the test would be real."

Only on a yes:
1. Call `alarm_control_panel.alarm_arm_away` from **Developer tools → Actions**.
2. Confirm `arming`, then `armed_away` within about 2 s of the exit delay ending.
3. Call `alarm_disarm`.
4. Confirm `disarmed`.

- [ ] **Step 4: External change detection (ask first)**

Ask the maintainer to arm and then disarm from the **Protect app**. Confirm that HA follows each change within about 2 s, with no poll needed. The HA debug log should show the coordinator being "Manually updated", not waiting for a poll.

- [ ] **Step 5: Optional checks (ask; skip any the maintainer declines)**

Record each result in spec §3 "Not yet verified", marking every item verified, unverified or changed:
- **Activation delay off:** the maintainer turns off the profile's Activation Delay in Protect. Arm from HA, and check the panel goes straight to `armed_away`. Restore the delay afterwards.
- **Breach:** only if the maintainer volunteers to trigger a sensor while armed. Confirm `triggered`, then disarm.
- **MFA and rate-limit shapes:** don't test these deliberately; they would lock the account out.

- [ ] **Step 6: Stop the dev instance and commit the spec update**

```bash
git add docs/superpowers/specs/2026-09-27-unifi-protect-alarm-bridge-design.md
git commit -m "docs: record real-console verification results

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Report to the maintainer:
- what passed
- what was skipped
- any spec changes

Then offer to open a pull request. Do not tag or release.
