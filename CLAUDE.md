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
