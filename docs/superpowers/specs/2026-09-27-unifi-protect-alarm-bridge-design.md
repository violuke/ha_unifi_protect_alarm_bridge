# UniFi Protect Alarm Bridge — Design

**Date:** 2026-09-27 (revised after spec review)
**Status:** Approved in brainstorming; review findings incorporated; pending maintainer review of the revision
**Domain:** `unifi_protect_alarm_bridge`
**Repo:** `github.com/violuke/ha_unifi_protect_alarm_bridge` (default branch `main`)

## 1. Problem

UniFi Protect's Alarm Manager switches to **Global** mode when sensors, relays, fobs or an Alarm Hub are adopted, and can revert after firmware updates. In Global mode the public Protect API offers no way to arm, disarm or read arm state. As a result, HA's official `unifiprotect` integration cannot expose a working alarm panel.

This integration provides an `alarm_control_panel` that works in Global mode. It runs alongside the official integration and does not depend on `uiprotect`.

## 2. How we got here (evidence, not assumptions)

The original brief proposed webhook-trigger alarms for writes and `nvr.armMode` from bootstrap for reads. Testing on a real console (UDM Pro, Protect 7.2.105, Global mode) ruled that approach out:

| Finding | Evidence |
|---|---|
| `nvr.armMode.status` is stuck at `"disabled"` in Global mode, in both the session bootstrap and the public `/integration/v1/nvrs`. It stayed there through a full arm → armed → disarm cycle. | Live polling during an arm cycle |
| Webhook-trigger alarms do fire (204 with `X-API-KEY`), but only in one direction: there is no way to read state back. The call returns 403 if a session cookie is also sent. | Live test; Alarm Manager execution stats |
| The real arm state is held by UniFi OS's Alarm Manager API, `/api/v2/alarms/profiles`. Its states are `disarmed`, `arming`, `armed` and `breached`. | Browser HAR; UI bundle enum `ARMED/ARMING/BREACHED/DISARMED` |
| The same API arms and disarms directly: `POST /api/v2/alarms/profiles/{id}/actions/arm` and `/disarm`. | HAR; headless live test |
| The API requires the **Super Admin** role. Custom roles get 403, including one with Protect Full Management, Network Site Admin and user-management View Only. | Live tests of each role |
| Protect's `wss://<host>/proxy/protect/ws/updates` pushes `update:externalArmProfile` messages. Each one carries the full profile object and arrives within about 1 s of the change. | Live websocket spike |
| Alarm Manager has an outbound Custom Webhook action, but no trigger for profile state changes. Push by webhook is therefore not possible. | `/api/v2/alarms/protect/manifest` |

**Result:** no API key and no webhooks. The integration uses one Super Admin local account. It reads and writes through the Alarm Manager REST API and receives push updates over the Protect websocket.

## 3. Verified API contract

All requests go to `https://<host>`. Consoles use self-signed certificates.

### Authentication
- `POST /api/auth/login` with `{"username", "password", "rememberMe": true}`. On success it returns 200, sets the `TOKEN` cookie and returns an `X-CSRF-Token` response header.
- **The cookie jar must accept cookies from IP-address hosts.** aiohttp's default jar rejects them, and most consoles are addressed by IP. The probes used `aiohttp.CookieJar(unsafe=True)`, as does the official `unifiprotect` integration.
- All later requests send the `TOKEN` cookie. State-changing requests also send `X-CSRF-Token`.
  - No `Origin` header was needed, and the CSRF token did not rotate during testing.
  - The client still adopts `X-Updated-CSRF-Token` when present.
  - The client always replaces its stored CSRF token with the one from each login response. If that header is missing, it falls back to the `csrfToken` claim in the TOKEN JWT, as uiprotect does.
- **Wrong credentials on login return 403**, not 401 (verified live). A login response of 400, 401 or 403 is `AuthFailed`.
- A 401 on any later request means the session has expired, so the client re-logs in (see §4 session rules).
- A 403 is only classified as "not Super Admin" when `GET /profiles` returns 403 **immediately after a fresh login**. A 403 on a POST first triggers one re-login and retry, because a stale CSRF token may also produce 403.

### Read state
`GET /api/v2/alarms/profiles` returns a list:
```json
[{"id": "<uuid>", "title": "Home alarm", "activation_delay": 60,
  "alarm_ids": ["<uuid>", "<uuid>"], "created_at": "...", "updated_at": "...",
  "state": "arming", "state_set_at": "2026-09-27T13:55:44Z",
  "state_promotion_due_at": "2026-09-27T13:56:44.585Z"}]
```
- `activation_delay` is the exit delay in seconds. The user can configure it as 1, 5 or 10 minutes, or turn it off (`null`/`0`), so it is never hard-coded.
- `state_promotion_due_at` is only present while `arming`. At that time the profile becomes `armed`. Verified: promotion happened at +60.4 s for a 60 s delay.
- `state_set_at` has 1-second resolution. It is not used to order updates (see §4 C2 guard).

### Write state
- `POST /api/v2/alarms/profiles/{id}/actions/arm` returns 200 and the updated profile, with `state: "arming"` (or `armed` when there is no delay; not yet verified).
- `POST /api/v2/alarms/profiles/{id}/actions/disarm` returns 200 and the updated profile, with `state: "disarmed"`.

### Push
- **Endpoint:** `wss://<host>/proxy/protect/ws/updates`, authenticated with the session cookie. No `lastUpdateId` is needed.
- **Message format:** each binary message holds two frames, an action frame followed by a data frame. Each frame is:
  - an 8-byte header (`>BBBBI`): packet type, payload format [1=JSON, 2=UTF-8, 3=buffer], deflated flag, reserved, payload size
  - then the payload, zlib-compressed if the deflated flag is set
- **Relevant message:** action `{"action": "update", "modelKey": "externalArmProfile", "id": "<profile id>"}`, whose data frame is the **full profile object** in the same shape as REST.
- **The pushed profile omits `state_promotion_due_at`** (verified live 2026-09-27). The coordinator derives it as `state_set_at + activation_delay` for `arming` profiles.
- **Also observed:** `add:event` with `type: "arming"` and `metadata.armProfileId`. This is not needed, because the profile update is the source of truth.
- **Volume:** the socket is busy, with about 180 messages per 100 s on the test site. Anything that is not `externalArmProfile` is discarded as soon as its action frame has been read.

### Console info (verified)
`GET /proxy/protect/api/nvr` (about 12 KB, session auth) returns what the integration needs:
- `mac`, used as the unique ID via `format_mac`
- `name`
- `type` (the model)
- `version` (the Protect version)
- `featureFlags.useExternalAlarmManager`, which is `true` in Global mode

### Resolved during planning
- **Unique ID:** `nvr.mac`, as above.
- **`lastUpdateId`:** `ws/updates` connects and streams without it (verified), so bootstrap is never fetched.
- **Global mode off:** detected with `featureFlags.useExternalAlarmManager == false` rather than by guessing at the shape of `/profiles`.

### Verified on the real console (2026-09-27)
- Setup with a local Super Admin account.
- Websocket push connected.
- HA arm → `arming` instantly, `armed` at +60 s via push; disarm instant.
- Arm/disarm from the Protect app reached HA within the same second.
- Websocket pushes lack `state_promotion_due_at` (handled — see §3 "Push" above).

### Not yet verified (resolve during the build; see §11)
1. **The `breached` payload.** It is deliberately not triggered on a live system, and is assumed to follow the UI enum.
2. **Arming with activation delay off.** The expected response is `armed` immediately.
3. **Arming profile B while profile A is armed.** Protect may reject the request, switch profiles, or allow both. The test console has one profile, so this may stay unverified; the entity behaviour in §4 is safe in all three cases.
4. **MFA challenge response shape.** UniFi OS is believed to return HTTP 499 with `MFA_AUTH_REQUIRED`. Until it is verified, detection matches HTTP 499. Any other unrecognised login status raises `UnexpectedResponse`, which the config flow reports as `unknown`.
5. **Login rate limiting.** UniFi OS is believed to return 429 (`AUTHENTICATION_FAILED_LIMIT_REACHED`). It is handled as a transient error either way.

## 4. Architecture

```
alarm_control_panel.py  ──reads──▶  coordinator.py  ◀──push──  websocket.py
        │                               │                          │
        └──arm/disarm──▶ coordinator ──▶ api.py ◀──poll────────────┘
                                         ▲ one session, one login lock
```

### `api.py` — `UniFiAlarmClient`

**Session**
- A dedicated session: `async_create_clientsession(hass, verify_ssl=<option>, cookie_jar=aiohttp.CookieJar(unsafe=True))`.
  - It is not HA's shared session, so its cookies stay isolated.
  - It uses the default `auto_cleanup=True`. When the session is created during `async_setup_entry`, HA registers `config_entry.async_on_unload` to close it (verified in HA 2025.11 and 2026.9 source), so reloads don't leak sessions. The config flow's short-lived validation session uses `auto_cleanup=False` and is released with `session.detach()` in a `finally` block. HA replaces `close()` on its sessions with a warning, so `close()` must not be used.
- `verify_ssl` defaults to off.

**Methods**
- `async_login()`, `async_get_console_info() -> ConsoleInfo`, `async_get_profiles() -> list[ArmProfile]`, `async_arm(profile_id) -> ArmProfile`, `async_disarm(profile_id) -> ArmProfile`.
- `ArmProfile` is a frozen dataclass parsed from JSON. REST and websocket share the same parser.

**Session rules**
- Login is guarded by an `asyncio.Lock` and a session `generation` counter.
  - A caller that received a 401 while on generation N only logs in if the current generation is still N. Otherwise it simply retries with the newer session.
  - This prevents parallel logins that overwrite TOKEN/CSRF pairs.
- A request gets at most one re-login and retry on 401.
- A POST gets at most one re-login and retry on 403, before the 403 is classified (see §3).

**Typed errors**

| Error | Meaning |
|---|---|
| `AuthFailed` | Bad credentials |
| `MfaRequired` | The account has 2FA |
| `RateLimited` | 429 on login; transient, never triggers reauth |
| `InsufficientPermissions` | 403 on `GET /profiles` immediately after a fresh login |
| `CannotConnect` | Network, TLS or timeout |
| `UnexpectedResponse` | Schema drift; carries the raw payload, which is redacted before it is logged or put in diagnostics |

### `websocket.py` — `ProtectUpdatesListener`

**Lifecycle**
- Started with `entry.async_create_background_task`, so it is cancelled on unload.
- Connects using the client's session with `ws_connect(..., heartbeat=30)`.

**Message handling**
- Decodes frames and calls `on_profile(ArmProfile)` for `externalArmProfile` updates only.
- Calls `on_connected()` on every successful connect or reconnect.

**Liveness**
- No messages for more than 2 minutes on this busy socket is treated as a dead connection: close and reconnect. This catches half-open TCP connections that the heartbeat misses.
- Reconnects with exponential backoff, from 1 s up to 5 min.

**Authentication**
- The listener never calls the login endpoint directly. On a 401 during the handshake it calls `client.async_login()`, which goes through the lock.
- If that raises `AuthFailed`, the listener **stops**, reports to the coordinator, and does not retry.
- `RateLimited` backs off like a network error.

**Logging and diagnostics**
- One warning when the socket drops and one info message on recovery, not one per retry.
- Tracks `connected`, `last_message_at` and `reconnect_count` for diagnostics.

### `coordinator.py`

`DataUpdateCoordinator[dict[str, ArmProfile]]`, keyed by profile id, constructed with `config_entry=`.

**Update sources and stale-write guard**
- There are three writers:
  - push (`on_profile`)
  - action responses (from arm/disarm)
  - polls (`_async_update_data`)
- The coordinator keeps a monotonic `write_seq` counter and records `profile_written_seq[id]` for each profile.
  - Push and action writes increment `write_seq` and stamp each profile they write.
  - A poll records `write_seq` when it starts. When the result arrives, a profile from the poll is **ignored** if that profile was stamped after the poll started. Profiles that are new or were deleted in the poll are always applied.
  - This stops an in-flight poll from overwriting a newer push or action result, such as a stale `disarmed` arriving while the system is arming.
- Arm and disarm go through coordinator methods (`async_arm(mode)`, `async_disarm()`). These call the API and write the returned profile under the guard.

**Polling safety net**
- `update_interval` is 60 s while the socket is healthy and 10 s while it is down. These values are hard-coded; there is no user option.
- When the socket's health changes, the coordinator sets `update_interval` and then calls `async_request_refresh()`, so the new interval takes effect straight away. Setting the attribute alone does not reschedule the pending timer.
- **On every (re)connect**, `on_connected()` calls `async_request_refresh()`. This catches changes made while the socket was down.

**Promotion refresh**
- While any profile is `arming`, the coordinator schedules one refresh with `async_call_later`.
  - The delay is `max(due − now, 0) + 2 s`, with a minimum of 5 s.
  - The callback is registered through `entry.async_on_unload`.
  - New data for that profile cancels and reschedules it.
- If three refreshes in a row still show `arming` past its due time (for example because of clock skew), the coordinator stops scheduling for that profile and relies on normal polling.

**Error mapping for polls**

| Error | Result |
|---|---|
| `AuthFailed` | `ConfigEntryAuthFailed`, which starts HA's built-in reauth |
| `InsufficientPermissions` | Repair `not_super_admin` + `UpdateFailed` |
| `CannotConnect` / `RateLimited` | `UpdateFailed`; the entity goes unavailable |
| `UnexpectedResponse` | Repair `api_changed` (Protect version, GitHub issues link) + `UpdateFailed` |
| Empty profile list or `UnexpectedResponse`, and `/nvr` shows `useExternalAlarmManager == false` | Repair `global_mode_off` + `UpdateFailed` |

**Websocket auth failure**
- When the listener reports `AuthFailed`, the coordinator calls `entry.async_start_reauth(hass)`.
- Reauth completes by reloading the entry, which restarts the listener.

**Availability**
- Push data sets `last_update_success=True`. This is accepted: a successful push proves the session and the console are working. It is safe because of the stale-write guard.

**Repair issue lifecycle**
- Issue IDs include the entry ID, for example `not_super_admin_<entry_id>`, so multiple consoles are supported.
- Each issue is deleted on the next successful poll where its condition no longer holds.
- All of an entry's issues are deleted in `async_remove_entry`.
- `push_unavailable` is raised once the socket has been down for more than 1 hour while polling is succeeding, and is cleared on reconnect.
- `profile_missing` is raised when a mapped profile id is absent from a successful poll. It has `is_fixable=True` (see §5).

### `alarm_control_panel.py`

**Entity and device**
- One entity per config entry (console), with `has_entity_name`.
- `DeviceInfo` includes the console name, model, `sw_version` (Protect version, if cheaply available) and `configuration_url=https://<host>`.

**Choosing the effective profile.** When several profiles exist, the entity reports the most severe one:
- **Precedence:** `breached` > `arming` > `armed` > `disarmed`.
- **Ties:** a mapped profile beats an unmapped one, then the profile id decides so the result is stable.
- If every profile is `disarmed`, the effective profile is the away-mapped one.

**State mapping** (from the effective profile)

| Protect `state` | HA `AlarmControlPanelState` |
|---|---|
| `disarmed` | `DISARMED` |
| `arming` | `ARMING` |
| `armed` | `ARMED_AWAY`, `ARMED_HOME` or `ARMED_NIGHT` (per the profile's mapping); `ARMED_CUSTOM_BYPASS` if the profile is unmapped (for example, armed from the Protect app) |
| `breached` | `TRIGGERED` |
| anything else | `None` (unknown), with a warning logged once per distinct value |

**Attributes and features**
- Extra state attributes all come from the **effective profile**: `profile_title`, `activation_delay`, `state_set_at`, `state_promotion_due_at`.
- Supported features follow the mapping: `ARM_AWAY` always, plus `ARM_HOME` and `ARM_NIGHT` when mapped.

**Arm (mode M)**
1. If any *other* profile is not `disarmed`, disarm it first. This keeps behaviour defined whichever way §3 item 3 turns out.
2. Arm the mapped profile.
3. If the mapped profile is missing, raise `HomeAssistantError` (translation key `profile_missing`).

**Disarm**
- Disarms every profile that is not `disarmed`.
- If all profiles are already `disarmed`, nothing is sent.
- Each profile is attempted even if an earlier one fails. Any failures are raised together as one `HomeAssistantError` that names the failed profiles.

**Errors and codes**
- API errors raise `HomeAssistantError` with translation keys, so the user sees a toast notification.
- `code_arm_required = False`, with no code format, because Protect has no PIN. HA permissions control access.

## 5. Config flow

**`async_step_user`**
- Fields: host, username, password, and "Verify SSL certificate" (default off).
- Validation runs login, then `GET /proxy/protect/api/nvr` (console info and Global-mode flag), then `GET /profiles`.

| Result | Error key |
|---|---|
| 401/400 on login | `invalid_auth` |
| MFA challenge | `mfa_not_supported` |
| 429 on login | `rate_limited` ("Too many failed logins; wait a few minutes") |
| Network, TLS or timeout | `cannot_connect` |
| 403 on profiles | `not_super_admin` |
| `useExternalAlarmManager` is false | `global_mode_off` |
| Empty profile list | `no_profiles` |
| Anything else | `unknown` (the exception is logged) |

- The unique ID is the console ID. Duplicates are blocked with `_abort_if_unique_id_configured`.

**`async_step_profiles`**
- Asks for the `arm_away` profile (required), plus optional `arm_home` and `arm_night` profiles.
- Skipped when there is only one profile; that profile is auto-mapped to away.
- A profile can be mapped to at most one mode, so `armed` resolves to exactly one HA state. A duplicate mapping shows the error `duplicate_profile`.

**`async_step_reauth` / `reauth_confirm`**
- Asks for username and password only.
- The unique ID must match (`_abort_if_unique_id_mismatch`).
- On success the entry is updated and reloaded.

**`async_step_reconfigure`**
- Asks for host and "Verify SSL certificate", then validates again.
- `_abort_if_unique_id_mismatch` makes sure the new host is the same console.

**Options flow**
- Profile mapping only, with the same form and validation as `async_step_profiles`.
- Saving reloads the entry.

**Repairs fix flow**
- For `profile_missing`, the fix flow shows the mapping form again.
- Until it is fixed, arming a missing mode raises the `profile_missing` error. The entity itself stays available, because it can still show state and disarm.

**Translations**
- All user-facing text lives in `translations/en.json`, the only copy that a custom integration uses at runtime. There is no separate `strings.json` to keep in sync.

## 6. Diagnostics

`diagnostics.py` returns:
- config entry data and options
- the last `ArmProfile` map
- websocket status (connected, last message time, reconnect count)
- last successful poll time
- Protect and UniFi OS versions, if cheaply available

Redacted with `async_redact_data`:
- `password`, `username`, `host`
- cookies and CSRF tokens
- profile `title`s, which can contain names or addresses

`UnexpectedResponse` payloads go through the same redaction.

## 7. Repo layout and packaging

Follows `ludeeus/integration_blueprint` conventions, but written by hand rather than generated from the template.

```
custom_components/unifi_protect_alarm_bridge/
  __init__.py  manifest.json  const.py  api.py  websocket.py  coordinator.py
  alarm_control_panel.py  config_flow.py  diagnostics.py  repairs.py
  translations/en.json  brand/icon.png  brand/icon@2x.png
tests/                       # pytest-homeassistant-custom-component
scripts/develop              # run a local HA with the integration mounted
config/configuration.yaml    # dev HA config; all other config/* gitignored
hacs.json  pyproject.toml  requirements_test.txt
.github/workflows/validate.yml  .github/workflows/release.yml
README.md  CONTRIBUTING.md  LICENSE (MIT)  CLAUDE.md
```

**`manifest.json`**
- `domain`, `name`, `config_flow: true`, `iot_class: local_push`, `integration_type: hub`
- `requirements: []`, because aiohttp ships with HA
- `version: 0.1.0`, `codeowners: ["@violuke"]`
- `documentation` and `issue_tracker` URLs

**`hacs.json`**
- `name`, `render_readme: true`, `homeassistant: "2026.9.0"`, `zip_release: true`, `filename: "unifi_protect_alarm_bridge.zip"`.

**Local `brand/` folder**
- If the running HA version does not read it, the icon is simply missing, which is harmless.
- If `hacs/action`'s brands check rejects a repo that is not in home-assistant/brands, set `ignore: brands` in `validate.yml` and note why.

**Platform**
- Minimum HA 2026.9 and Python 3.14. Older releases are not supported.
- Uses `entry.runtime_data`, the `AlarmControlPanelState` enum and a typed `ConfigEntry`.

**`.gitignore`**
- Python artifacts
- `.env*` (except `.env.example`)
- `config/*` (except `configuration.yaml`)
- `.venv`, `.pytest_cache`, `.ruff_cache`, `*.har`

## 8. Testing

**Framework:** `pytest-homeassistant-custom-component`, pinned to `0.13.367` (HA 2026.9.4).

**Fixtures**
- Sanitised copies of the real payloads: profiles, arm/disarm responses, and `externalArmProfile` frames.
- Placeholder values only: placeholder IDs, host `192.0.2.1`, and credentials `test-user` / `test-password`.

**Coverage**
- **Config flow:** every branch, plus reauth, reconfigure (including the unique-ID-mismatch abort) and options.
- **API:**
  - Login and CSRF handling, including the JWT fallback.
  - Re-login on 401, and re-login and retry on 403 for POSTs.
  - **Concurrent 401s cause exactly one login.**
  - Each error type, and schema drift.
  - **A real `ClientSession` with the unsafe cookie jar against an IP host, to prove the TOKEN cookie is sent back.**
- **Websocket:**
  - Frame decoding, both deflated and plain, and filtering.
  - Reconnect backoff.
  - The silence timeout.
  - Auth failure → stop and start reauth.
  - Rate limit → backoff.
- **Coordinator:**
  - Push, fallback polling, and interval switching with an immediate refresh.
  - Refresh on reconnect.
  - Promotion refresh, including the clock-skew cap.
  - **A poll started before an arm and finishing after it does not overwrite the arm result.**
  - Repair create/delete lifecycle.
  - Unload tears down the task, timers and session.
- **Entity:**
  - The mapping table and effective-profile precedence.
  - An unmapped profile armed externally → `ARMED_CUSTOM_BYPASS`.
  - Arm disarms other active profiles first.
  - Disarm when already disarmed is a no-op.
  - Disarm partial-failure reporting.
  - Error toasts.
- **Diagnostics:** redaction, including host, titles and `UnexpectedResponse` payloads.

**Real-console testing** goes through `scripts/develop`. Credentials are entered in the config flow UI and stored only in the gitignored `config/.storage`, never in committed files.

## 9. CI and release

**`validate.yml`** runs on every push and PR:
- `home-assistant/actions/hassfest`
- `hacs/action` (category `integration`)
- `ruff check` and `ruff format --check`
- `pytest`

**`release.yml`** runs on a `v*` tag. It:
1. Stamps the `manifest.json` version from the tag.
2. Zips the contents of the integration directory.
3. Creates a GitHub Release with the zip attached.
4. Marks tags with a pre-release suffix (e.g. `v0.1.0-beta1`) as a **pre-release**.

**Pre-release visibility.** HACS hides pre-releases by default. While the only release is a pre-release, the README tells testers to enable "Show beta versions" for this repository in HACS. The maintainer decides later when to cut a normal release.

**Branching:** `main` plus feature branches. PRs are gated by `validate`.

**Tags and releases:** none are created until the maintainer explicitly asks. The first will be `v0.1.0-beta1`, as a pre-release.

## 10. Documentation

**README**
- **Prerequisites:**
  - Global Alarm Manager with at least one arm profile.
  - A **dedicated local-only Super Admin account without MFA**, with an explanation of why no lesser role works.
- **Security note:**
  - The credentials are stored in plaintext in HA's `.storage`.
  - A Super Admin account controls the whole console: network, cameras and users. This is why the account should be dedicated, local-only and unused for anything else.
- **Install:** HACS custom repository, plus the beta-visibility note.
- **Setup:** a config flow walkthrough.
- **Troubleshooting:**
  - Entity unavailable → network or credentials; check Repairs.
  - `not_super_admin` → the account's role.
  - `global_mode_off` → the Alarm Manager mode.
  - `push_unavailable` → a firmware change.
  - Arm does nothing → check the profile mapping and Protect's arm profile.
- **Disclaimer:** the integration relies on undocumented UniFi APIs.

**CONTRIBUTING**
- Running the tests locally.
- How validation gates PRs.
- Using `scripts/develop`.
- The tag and release convention.

**CLAUDE.md** is updated with commands and architecture once the code lands.

## 11. Build order

1. **Scaffold.** Create the directory structure, `manifest.json`, `hacs.json`, `.gitignore`, `pyproject.toml`, `LICENSE`, and a **minimal config flow stub with `translations/en.json` plus one smoke test**. Then add `validate.yml`, so that hassfest and pytest pass from the first commit and every commit after is checked.
2. **`api.py` and `websocket.py`**, with unit tests against recorded fixtures.
3. **`coordinator.py`**, tested against mocked profile and websocket data.
4. **The rest of the integration:** `config_flow.py` in full, then `alarm_control_panel.py`, `diagnostics.py` and `repairs.py`.
5. **Real-console checks** via `scripts/develop` for the remaining §3 items. Ask before any arm or disarm test.
6. **Docs and release:** README, CONTRIBUTING, brand icon, `release.yml`, CLAUDE.md.

## 12. Out of scope (v1)

- API key and webhook-based control (superseded).
- A user-configurable poll interval (push is primary; polling is a hard-coded safety net).
- Editing or creating Alarm Manager alarms or profiles.
- Per-sensor entities, siren control and bypass zones.
- An HA-side PIN code.
- Multiple consoles in one config entry (add one entry per console instead).
