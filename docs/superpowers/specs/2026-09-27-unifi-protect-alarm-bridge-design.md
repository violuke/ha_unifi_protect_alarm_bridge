# UniFi Protect Alarm Bridge — Design

**Date:** 2026-09-27
**Status:** Approved in brainstorming; pending written-spec review
**Domain:** `unifi_protect_alarm_bridge`
**Repo:** `github.com/violuke/ha_unifi_protect_alarm_bridge` (default branch `main`)

## 1. Problem

UniFi Protect's Alarm Manager switches to **Global** mode when sensors, relays, fobs or an Alarm Hub are adopted, and it can revert after firmware updates. In Global mode the public Protect API gives no way to arm, disarm or read arm state, so HA's official `unifiprotect` integration can't expose a working alarm panel.

This integration provides an `alarm_control_panel` that works in Global mode. It runs alongside the official integration and doesn't depend on `uiprotect`.

## 2. How we got here (evidence, not assumptions)

The original brief proposed webhook-trigger alarms for writes and `nvr.armMode` from bootstrap for reads. We tested it against a real console (UDM Pro, Protect 7.2.105, Global mode) and rejected it:

| Finding | Evidence |
|---|---|
| `nvr.armMode.status` is frozen at `"disabled"` in Global mode, in both session bootstrap and the public `/integration/v1/nvrs`. It didn't change through a full arm → armed → disarm cycle. | Live polling during arm cycle |
| Webhook-trigger alarms do fire (204 with `X-API-KEY`), but the path is write-only. The request fails with 403 if a session cookie is also sent. | Live test; Alarm Manager execution stats |
| Real arm state lives in UniFi OS's Alarm Manager API, `/api/v2/alarms/profiles`, with states `disarmed`, `arming`, `armed`, `breached`. | Browser HAR; UI bundle enum `ARMED/ARMING/BREACHED/DISARMED` |
| That API also arms and disarms directly: `POST /api/v2/alarms/profiles/{id}/actions/arm` and `/disarm`. | HAR; headless live test |
| The API needs the **Super Admin** role. Custom roles fail with 403, including one with Protect Full Management, Network Site Admin and user-management View Only. | Live tests of each role |
| Protect's `wss://<host>/proxy/protect/ws/updates` pushes `update:externalArmProfile` messages carrying the full profile object within about 1 s of a change. | Live websocket spike |
| Alarm Manager has an outbound Custom Webhook action, but no trigger for profile state changes, so webhook push isn't possible. | `/api/v2/alarms/protect/manifest` |

**Result:** no API key and no webhooks. We use one Super Admin local account, the Alarm Manager REST API for reads and writes, and the Protect websocket for push.

## 3. Verified API contract

All requests go to `https://<host>`. Consoles use self-signed certificates.

### Authentication
- `POST /api/auth/login` with `{"username", "password", "rememberMe": true}` returns 200, sets the `TOKEN` cookie and returns an `X-CSRF-Token` response header.
- For all later requests, send the `TOKEN` cookie. State-changing requests also need `X-CSRF-Token`. No `Origin` header is needed, and the CSRF token did not rotate in testing. The client still adopts `X-Updated-CSRF-Token` when present.
- 401 means the session has expired: log in again once, then retry.
- 403 after a successful login means the account lacks Super Admin.

### Read state
`GET /api/v2/alarms/profiles` returns a list of profiles:
```json
[{"id": "<uuid>", "title": "Home alarm", "activation_delay": 60,
  "alarm_ids": ["<uuid>", "<uuid>"], "created_at": "...", "updated_at": "...",
  "state": "arming", "state_set_at": "2026-09-27T13:55:44Z",
  "state_promotion_due_at": "2026-09-27T13:56:44.585Z"}]
```
- `activation_delay` is the exit delay in seconds. The user can set it to 1, 5 or 10 minutes, or off (`null`/`0`), so it's never hardcoded.
- `state_promotion_due_at` is only present while `arming`. At that time the state becomes `armed` (verified, +60.4 s for a 60 s delay).

### Write state
- `POST /api/v2/alarms/profiles/{id}/actions/arm` returns 200 and the updated profile (`state: "arming"`, or presumably `armed` when there's no delay).
- `POST /api/v2/alarms/profiles/{id}/actions/disarm` returns 200 and the updated profile (`state: "disarmed"`).

### Push
- `wss://<host>/proxy/protect/ws/updates[?lastUpdateId=<bootstrap.lastUpdateId>]`, authenticated with the session cookie.
- Binary messages hold two frames: an action frame and a data frame. Each frame is an 8-byte header (`>BBBBI`: packet type, payload format [1=JSON, 2=UTF-8, 3=buffer], deflated flag, reserved, payload size) followed by the payload, which is zlib-compressed if the deflated flag is set.
- Relevant message: action `{"action": "update", "modelKey": "externalArmProfile", "id": "<profile id>"}` with data being the **full profile object**, in the same shape as the REST response.
- Also observed: `add:event` with `type: "arming"` and `metadata.armProfileId`. It isn't needed, because the profile update is the source of truth.
- The socket is chatty (about 180 messages per 100 s on a busy site). Everything that isn't `externalArmProfile` is discarded after reading the action frame.

### Not yet verified
- `breached` state payload (deliberately not triggered on a live system). It's treated as expected from the UI enum.
- Arm with activation delay off. The expected response is `armed` immediately.
- Whether `ws/updates` needs `lastUpdateId`. The build tests this; if it's required, bootstrap (about 340 KB) is fetched once per reconnect, not per poll.
- The cheapest endpoint that exposes a stable console ID for the unique ID. Candidates: a UniFi OS system endpoint, otherwise `bootstrap.nvr.mac`.

## 4. Architecture

```
alarm_control_panel.py  ──reads──▶  coordinator.py  ◀──push──  websocket.py
        │                               │                          │
        └──arm/disarm──▶  api.py  ◀──poll──┘                         │
                              ▲──────────────── shared session ─────┘
```

### `api.py` — `UniFiAlarmClient`
- Pure aiohttp with no HA imports. It uses HA's shared session via `async_create_clientsession` with its own cookie jar, so it's isolated from other integrations.
- `async_login()`, `async_get_profiles() -> list[ArmProfile]`, `async_arm(profile_id) -> ArmProfile`, `async_disarm(profile_id) -> ArmProfile`.
- `ArmProfile` is a frozen dataclass parsed from JSON. The REST and websocket paths share the parser.
- Typed errors:
  - `AuthFailed`: bad credentials
  - `MfaRequired`: login response indicates a 2FA challenge
  - `InsufficientPermissions`: 403 after login
  - `CannotConnect`: network, TLS or timeout
  - `UnexpectedResponse`: schema drift; carries the raw payload for diagnostics
- Transparent single re-login on 401.
- `verify_ssl` option; default off.

### `websocket.py` — `ProtectUpdatesListener`
- Connects to `ws/updates` using the client's session, decodes frames, and calls `on_profile(ArmProfile)` for `externalArmProfile` updates only.
- Reconnects with exponential backoff from 1 s to 5 min, and re-logs in on a 401 during the handshake.
- Tracks `connected`, `last_message_at` and `reconnect_count` for diagnostics.
- Logs one warning when the socket drops and one info message when it recovers, never on every retry.

### `coordinator.py`
- A `DataUpdateCoordinator[dict[str, ArmProfile]]` keyed by profile ID.
- **Push:** the listener calls `async_set_updated_data()` with the merged profile map.
- **Polling safety net:** `GET /profiles` every 60 s while the socket is healthy, and at the configured interval while it's down. The configured interval defaults to 10 s, with a range of 10–300 s.
- **Promotion refresh:** when any profile is `arming`, the coordinator schedules a one-off refresh at `state_promotion_due_at + 2 s`. Any new data cancels and reschedules it.
- **Error mapping:**
  - `AuthFailed` → `ConfigEntryAuthFailed`, which triggers HA's built-in reauth
  - `InsufficientPermissions` → repair issue `not_super_admin`, plus `UpdateFailed`
  - `CannotConnect` → `UpdateFailed`, and the entity goes unavailable
  - `UnexpectedResponse` → repair issue `api_changed` with the Protect version and a GitHub issues link, plus `UpdateFailed`
- A repair issue `push_unavailable` is raised if the socket has been down for more than 1 hour while polling still succeeds. It clears on reconnect.

### `alarm_control_panel.py`
- One entity per config entry (console), with `has_entity_name` and a device named after the console.
- **State** comes only from coordinator data, never assumed:

  | Protect `state` | HA `AlarmControlPanelState` |
  |---|---|
  | `disarmed` | `DISARMED` |
  | `arming` | `ARMING` |
  | `armed` | `ARMED_AWAY`, `ARMED_HOME` or `ARMED_NIGHT`, depending on which mapped profile is armed |
  | `breached` | `TRIGGERED` |
  | anything else | `None` (unknown), with a warning logged once per value |

  If an unmapped profile is non-disarmed, the entity reports that state, using `ARMED_CUSTOM_BYPASS` for `armed`, and names the profile in an attribute. That covers profiles armed from the Protect app that HA doesn't know about.
- **Supported features** are derived from the mapping: `ARM_AWAY` always, plus `ARM_HOME` and `ARM_NIGHT` when mapped.
- **Arm** calls `async_arm(mapped_profile_id)`. **Disarm** calls `async_disarm()` on every profile that isn't `disarmed`, or on the away profile if all of them are disarmed.
- The action response (the updated profile) is pushed straight into the coordinator, so there's no extra poll and no optimistic state.
- API errors raise `HomeAssistantError` with a translation key, so the user sees a toast.
- `code_arm_required = False` and there's no code format, because Protect has no PIN. Access is controlled with HA permissions.
- Extra state attributes: `profile_title`, `activation_delay`, `state_set_at`, `state_promotion_due_at`.

## 5. Config flow

### `async_step_user`
- Fields: host, username, password, and "Verify SSL certificate" (default off).
- Validation performs login plus `GET /profiles`.

  | Result | Error key |
  |---|---|
  | 401/400 on login | `invalid_auth` |
  | 2FA challenge | `mfa_not_supported` |
  | Network, TLS or timeout | `cannot_connect` |
  | 403 on profiles | `not_super_admin` |
  | Empty profile list | `no_profiles` |
  | Anything else | `unknown`, with the exception logged |

- The unique ID is the console ID (see "Not yet verified"). `_abort_if_unique_id_configured` prevents duplicates.

### `async_step_profiles`
- Selects the `arm_away` profile (required), and optionally the `arm_home` and `arm_night` profiles.
- Skipped when there's only one profile, which is then auto-mapped to away.
- Each profile can be mapped to at most one mode, so `armed` resolves to exactly one HA state. A duplicate raises the error `duplicate_profile`.

### Other flows
- **`async_step_reauth` / `reauth_confirm`:** username and password only. The unique ID must match.
- **`async_step_reconfigure`:** host and "Verify SSL certificate"; re-validates.
- **Options flow:** poll interval (10–300, default 10) and profile mapping. Changes reload the entry.
- **Repairs fix flow** for `profile_missing`, raised when a mapped profile no longer exists: it re-runs the mapping form.

All user-facing text lives in `strings.json` and `translations/en.json`.

## 6. Diagnostics
`diagnostics.py` returns the following, with password, username, cookie and CSRF token redacted via `async_redact_data`:
- config entry data and options
- the last `ArmProfile` map
- websocket status (connected, last message time, reconnect count)
- last successful poll time
- Protect and UniFi OS versions, if cheaply available

## 7. Repo layout and packaging

Follows `ludeeus/integration_blueprint` conventions, written by hand (not generated from the template).

```
custom_components/unifi_protect_alarm_bridge/
  __init__.py  manifest.json  const.py  api.py  websocket.py  coordinator.py
  alarm_control_panel.py  config_flow.py  diagnostics.py  repairs.py
  strings.json  translations/en.json  brand/icon.png  brand/icon@2x.png
tests/                       # pytest-homeassistant-custom-component
scripts/develop              # run a local HA with the integration mounted
config/configuration.yaml    # dev HA config; all other config/* files gitignored
hacs.json  pyproject.toml  requirements_test.txt
.github/workflows/validate.yml  .github/workflows/release.yml
README.md  CONTRIBUTING.md  LICENSE (MIT)  CLAUDE.md
```

- **`manifest.json`:** `domain`, `name`, `config_flow: true`, `iot_class: local_push`, `integration_type: hub`, `requirements: []` (aiohttp ships with HA), `version: 0.1.0`, `codeowners: ["@violuke"]`, plus `documentation` and `issue_tracker` URLs.
- **`hacs.json`:** `name`, `render_readme: true`, `homeassistant: "2025.11.0"`, `zip_release: true`, `filename: "unifi_protect_alarm_bridge.zip"`.
- **Minimum HA version 2025.11 / Python 3.13.** Uses `entry.runtime_data`, the `AlarmControlPanelState` enum and a typed `ConfigEntry`.
- **`.gitignore`:** Python artifacts, `.env*` (except `.env.example`), `config/*` except `configuration.yaml`, `.venv`, `.pytest_cache`, `.ruff_cache`, `*.har`.

## 8. Testing

- `pytest-homeassistant-custom-component`, pinned to the release matching HA 2025.11.
- Fixtures are sanitised copies of the real payloads (profiles, arm/disarm responses, `externalArmProfile` frames), with placeholder IDs, host `192.0.2.1`, and credentials `test-user` / `test-password`. No real values are ever used.
- Coverage:
  - config flow: every branch, plus reauth, reconfigure and options
  - API: login and CSRF, 401 re-login, each error type, schema drift
  - websocket: frame decoding (deflated and plain), filtering, reconnect backoff
  - coordinator: push updates, fallback polling, the promotion refresh, interval switching, error-to-repair mapping
  - entity: the state-mapping table, unmapped profile armed externally, arm/disarm service calls and error toasts
  - diagnostics: redaction
- Real-console testing goes through `scripts/develop`. Credentials are entered in the config flow UI, stored only in gitignored `config/.storage`, and never written to committed files.

## 9. CI and release

- **`validate.yml`** (push and PR): `home-assistant/actions/hassfest`, `hacs/action` (category `integration`), `ruff check`, `ruff format --check`, and `pytest`.
- **`release.yml`** (tag `v*`):
  - stamps `manifest.json` version from the tag
  - zips the integration directory's contents
  - creates a GitHub Release with the zip attached
  - marks tags with a pre-release suffix (e.g. `v0.1.0-beta1`) as **pre-release**
- **Branching:** `main` plus feature branches, with PRs gated by `validate`.
- **No tags or releases are created until the maintainer explicitly asks.** The first will be `v0.1.0-beta1`, as a pre-release.

## 10. Documentation

**README:**
- Prerequisites: Global Alarm Manager with at least one arm profile, and a **dedicated local-only Super Admin account without MFA**, with an explanation of why no lesser role works.
- HACS custom-repository install.
- Config flow walkthrough.
- Troubleshooting:
  - entity unavailable → network or credentials; check repairs
  - `not_super_admin` → role
  - `push_unavailable` → firmware change
  - arm does nothing → check the profile mapping and Protect's arm profile
- A "this uses undocumented UniFi APIs" disclaimer.

**CONTRIBUTING:**
- running tests locally
- how validation gates PRs
- `scripts/develop`
- the tag and release convention

**CLAUDE.md** is updated with commands and architecture once code lands.

## 11. Build order

1. Scaffold: directory structure, `manifest.json`, `hacs.json`, `.gitignore`, `pyproject.toml`, LICENSE, and `validate.yml`. After this, every commit is checked.
2. `api.py` and `websocket.py` with unit tests against recorded fixtures.
3. `coordinator.py` tested against mocked profile and websocket data.
4. `config_flow.py` (plus strings), then `alarm_control_panel.py`, `diagnostics.py` and `repairs.py`.
5. Resolve the "Not yet verified" items against the real console via `scripts/develop`. Ask before any arm/disarm test.
6. README, CONTRIBUTING, brand icon, `release.yml`, CLAUDE.md.

## 12. Out of scope (v1)

- API key and webhook-based control (superseded).
- Editing or creating Alarm Manager alarms or profiles.
- Per-sensor entities, siren control, bypass zones.
- An HA-side PIN code.
- Multiple consoles per entry (add one entry per console instead).
