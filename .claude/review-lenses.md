# Review lenses: ha_unifi_protect_alarm_bridge

Read by the `breww-workflows:multi-agent-review` skill. The skill supplies the harness (scope, diff capture, dispatch, triage). This file supplies the opinions: which reviewers to run, what each one hunts for, and how to verify a fix.

This is a Home Assistant custom integration that arms, disarms and reports the state of a **security alarm**. The worst failures are these, and every lens should weigh findings against them:

1. the panel showing a wrong or stale state;
2. an arm or disarm that silently does nothing;
3. the Super Admin credentials leaking.

## Verification commands

Run these before dispatching and put the results in every agent prompt (the agents have no shell). The environment is `.venv` (Python 3.14, HA 2026.9.4 via `pytest-homeassistant-custom-component==0.13.367`). Create it with `uv venv .venv -p 3.14 && uv pip install --python .venv -r requirements_test.txt` if it's missing.

```bash
.venv/bin/pytest -q                  # full suite, a few seconds
.venv/bin/ruff check .
.venv/bin/ruff format --check .
uvx zizmor .github                   # only when .github/ changed
```

- hassfest and `hacs/action` run only in CI (`.github/workflows/validate.yml`). When `manifest.json`, `hacs.json`, `translations/` or the workflows change, say that CI is the proof, and check the latest run with `gh run list --branch <branch>`.
- Real-console behaviour can only be exercised with `scripts/develop` and the maintainer present. Never arm or disarm the real system without the maintainer's explicit OK at that moment.
- After fixing findings, re-run the suite and report the count.

## Sources for the "don't re-flag" list

- **Spec `docs/superpowers/specs/2026-09-27-unifi-protect-alarm-bridge-design.md`:**
  - §2 (why webhooks and `nvr.armMode` were rejected)
  - §3 "Not yet verified" (known open questions, not findings)
  - §12 "Out of scope (v1)"
- **Accepted decisions:**
  - The API account must be **Super Admin**. Custom roles get 403, which was verified live.
  - SSL verification is **off by default**, because consoles ship self-signed certificates.
  - Poll intervals are hard-coded at 60 s healthy / 10 s down, with no user option.
  - There is no HA-side PIN (`code_arm_required = False`).
  - `websocket.py` `_close()` cancels aiohttp's private `_heartbeat_cb`. It works around an aiohttp 3.14.x bug that re-arms the heartbeat after close. It's deliberate and `getattr`-guarded.
  - The device uses `connections={(CONNECTION_NETWORK_MAC, …)}` so it merges with the official UniFi Protect integration's console device. This is deliberate and documented in the README.
  - Unknown profile states rank above `disarmed` ("surface rather than hide").
  - Arming a target that is `arming`/`armed` is a silent no-op (double-tap). A `breached` target raises `disarm_first`.
  - Commit trailers name the model that wrote each commit.
- **CI:** the `hacs` job's `license` check fails on branches until `LICENSE` reaches `main`, because `hacs/action` reads the default branch.
- **Rulings:** any ruling recorded in a plan or SDD ledger for the work under review (`.superpowers/sdd/*/progress.md` when present).

## The lens set

| Lens | Skip when |
|---|---|
| A: Alarm-state truth | no change to `coordinator.py`, `alarm_control_panel.py`, `websocket.py`, or `api.py` parsing (`ArmProfile`/`ConsoleInfo`) |
| B: UniFi API contract & session | no change to `api.py` or `websocket.py` |
| C: Async lifecycle & concurrency | no change to async code, timers, background tasks, setup/unload, or locks |
| D: Home Assistant conventions & compatibility | no production code, `manifest.json` or `translations/` change |
| E: Security & privacy | no production code, diagnostics, logging, fixtures, `scripts/` or docs change |
| F: Test quality & effectiveness | no test file and no production code path changed |
| G: Docs & user contract | no README, CONTRIBUTING, CLAUDE.md, spec, `translations/` or user-visible behaviour change |
| H: Slop, simplification & house style | a one-or-two-line fix |
| I: Release, packaging & CI | no change to `.github/`, `hacs.json`, `manifest.json`, `pyproject.toml`, `requirements_test.txt`, `scripts/` or `.gitignore` |

Every lens must be told the following:
- Read surrounding and sibling code freely. The integration is small, so read the whole module a change lives in.
- Judge only what the diff changes: added lines and removed ones, since a deleted guard is a regression too. Pre-existing code is out of scope.
- `api.py` and `websocket.py` must never import `homeassistant`.

**Agent A: Alarm-state truth.** Could any change make the panel show a state the console isn't in, show it late, or make an arm/disarm silently do nothing? Trace every path that writes `coordinator.data`:
- the poll (`_async_update_data`)
- the websocket push (`handle_profile` via `ProtectUpdatesListener`)
- the arm/disarm action responses (`async_arm_profile`/`async_disarm_profile`)

Hunt for:
- **Stale-write guard gaps.** An older result overwriting a newer one, including an in-flight poll finishing after a push, a push after an action response, and a dropped profile being resurrected.
- **Push/REST shape differences.** Protect's websocket profile payloads omit fields the REST response has (`state_promotion_due_at` was verified missing live). Look for a push that erases data, or a push that's only partial.
- **Promotion refresh (`state_promotion_due_at`) problems.** It isn't scheduled, it's cancelled by an unrelated update, it gets stuck behind clock skew, it's reset by every push, or it runs after unload.
- **Entity mapping errors (`alarm_control_panel.py`).** Effective-profile precedence (breached > arming > armed > disarmed; mapped beats unmapped on ties; then id), the `armed` mode lookup, `ARMED_CUSTOM_BYPASS` for unmapped profiles, `None` for unknown states, attributes taken from a different profile than the state, and supported features vs the mapping.
- **Arm/disarm orchestration mistakes.** Stale snapshots (`target` captured before disarming others), errors swallowed or turned into success, disarm-all not attempting every profile, and a no-op that should be an error or vice versa.
- **Availability errors.** A failing poll hidden by a push, or a working push hidden by a failing poll.

Name the concrete interleaving or sequence of console events that produces the wrong state.

**Agent B: UniFi API contract & session.** Check every change against spec §3, which was verified live against a real console and describes the undocumented API. Hunt for:
- **Login and CSRF.** `POST /api/auth/login`, the `TOKEN` cookie plus the `X-CSRF-Token` header, adopting `X-Updated-CSRF-Token`, the JWT `csrfToken` fallback. A wrong password returns **403** at login.
- **Status classification.**
  - 401 → re-login once.
  - 403 straight after a fresh login → `InsufficientPermissions`.
  - 403 on an established session → re-login once (stale CSRF).
  - 429 → `RateLimited` (transient, never reauth).
  - 499 → `MfaRequired`.
  - Anything else → `UnexpectedResponse` with the payload.
- **Session setup.** Every session is `aiohttp.CookieJar(unsafe=True)`; the default jar drops cookies from IP hosts.
- **Login lock and generation counter.** Parallel 401s must cause exactly one login, a first-ever burst must dedupe, and a stale generation must skip.
- **Websocket frames.** Decoding of `ws/updates` binary frames (`>BBBBI` header, zlib, JSON/UTF-8), filtering to `externalArmProfile` `update` actions, undecodable or partial frames (skip or resync, never crash), and handshake 401/403 handling.
- **Parsing.** Robustness of `ArmProfile.from_api`/`ConsoleInfo.from_api` to schema drift: a missing or retyped field must become `UnexpectedResponse`, never a wrong value. Also 7-digit fractional timestamps and tz-awareness.
- **Assumptions the spec lists as unverified**, now being relied on.

For each finding, say what the console would have to send to trigger it.

**Agent C: Async lifecycle & concurrency.** Hunt for:
- **Leaks.** Tasks, timers (`async_call_later`), websockets or aiohttp sessions that outlive unload or reload.
  - The listener runs through `entry.async_create_background_task`.
  - The session is created inside `async_setup_entry`, so HA's `auto_cleanup` closes it.
  - The config-flow validation session must use `detach()` (HA wraps `close()` with a warning).
  - `async_shutdown` must cancel the promotion timer.
- **Post-shutdown work.** `_shutdown_requested` guards on anything a late push or callback can trigger.
- **Cancellation.** `CancelledError` must propagate and never be caught by `except Exception`. Cleanup in `finally` must not raise.
- **Reconnect behaviour.** Backoff growth, cap and reset (including after an established connection ends by exception), tight loops, the silence timeout vs the heartbeat, and warning-once logging.
- **Login storms and account lockout.** Bad credentials must stop the listener and start reauth exactly once. `RateLimited` backs off. A misconfigured account mustn't cause a login per poll.
- **Coordinator interaction.** Changing `update_interval` does not reschedule the timer, so it needs a refresh. `async_set_updated_data` cancels a pending debounced refresh. The debouncer cooldown and `async_request_refresh` vs `async_refresh` also matter.
- **Awaits in the middle of a read-modify-write** on shared state.

Cite the HA or aiohttp source line (under `.venv/lib/python3.14/site-packages/`) for any claim about framework behaviour.

**Agent D: Home Assistant conventions & compatibility.** Supported: **Home Assistant 2026.9+ on Python 3.14 only**. Don't add shims for older releases, and flag any that appear. Check against the installed HA source (`.venv/lib/python3.14/site-packages/homeassistant/`). Hunt for:
- **Deprecated or private HA APIs.**
- **Config entries.** `runtime_data` typing (`UniFiAlarmConfigEntry`), setup errors (`ConfigEntryAuthFailed` vs `ConfigEntryNotReady`), unload/remove.
- **Flows.** User/profiles/reauth/reconfigure/options (`OptionsFlowWithReload`): `_abort_if_unique_id_mismatch`, `async_update_reload_and_abort`, the unique ID from `format_mac(nvr.mac)`, host normalisation.
- **Repair issues.** Entry-ID-suffixed IDs, a create/delete rule for each, fixable-issue data, the fix flow (`RepairsFlow`/`RepairsFlowResult`), and deletion in `async_remove_entry`.
- **Entity conventions.** `has_entity_name`, `_attr_name = None`, `unique_id`, `DeviceInfo`, `PARALLEL_UPDATES`, `code_arm_required`, `AlarmControlPanelState`/`AlarmControlPanelEntityFeature`.
- **Translations.** Every error, abort, issue, exception and placeholder key used in code must exist in `translations/en.json` with matching placeholders. There is no `strings.json`.
- **`manifest.json` rules.** Key order, `iot_class: local_push`, `requirements: []`, and nothing that makes hassfest fail.
- **Blocking I/O in the event loop.**

Name the HA version or source line when a behaviour depends on it.

**Agent E: Security & privacy.** The integration holds a **Super Admin** credential for the whole console (network, cameras, users). Hunt for:
- **Credentials and tokens reaching places they shouldn't.** Username, password, the `TOKEN` cookie or the CSRF token must not end up in logs (including `UnexpectedResponse` payloads, which must go through `REDACT_KEYS`), exceptions shown to users, repair-issue placeholders, entity attributes or diagnostics.
- **Diagnostics redaction** (`REDACT_KEYS`, `async_redact_data`). It must cover host, username, password, console name, profile titles, and cookie/CSRF/token keys, including in nested payloads.
- **TLS.** Anything that weakens verification when the user turns it on, and anything that sends credentials over plain HTTP.
- **Secrets in the repo.** Real hosts, IPs, MACs, profile IDs, names or credentials in committed files, test fixtures, docs or scripts. Tests must use the placeholders in `tests/helpers.py`: `192.0.2.1`, `test-user`, `test-password`, `00000000-0000-4000-8000-00000000000X`.
- **Untrusted console data.** Payloads used as format strings, as log injection, or in file paths.
- **Dev harness.** `scripts/develop` and `.gitignore` must keep `config/.storage`, `.env` and `*.har` out of git.

**Agent F: Test quality & effectiveness.** Would each new or changed test fail if the behaviour it names broke? Hunt for:
- **Assertions that can't fail.** Asserting on a mock's configured return value, `assert_not_awaited` on an entity that doesn't exist (a service call to a missing entity is a silent no-op), assertions after an exception that never ran.
- **Mocks where the real thing is needed.**
  - Client and websocket behaviour belongs against `tests/fake_console.py`, a real aiohttp server with a real cookie jar and a real websocket.
  - Coordinator and entity tests may mock the client, but must drive real HA machinery.
- **Timing fragility.** Sleeps instead of `wait_until` / `async_fire_time_changed`, and ordering assumptions between tasks.
- **Cleanup.** PHACC fails on lingering tasks and timers. Fixtures must shut the coordinator down and cancel listener tasks.
- **Isolation gaps.** Tests that only pass in a full run because another test imported something. For example, poll-driven reauth needs `config_flow` imported.
- **Coverage of the changed branch.** For each changed branch, name the test that exercises it. Where none does, propose one. Where the right check is a mutation ("break this line and this test must fail"), say which.

Test style for this repo:
- plain `async def test_…(hass, …) -> None` functions
- `pytest.mark.parametrize` for tables
- helpers and placeholders from `tests/helpers.py`
- fixtures from `tests/conftest.py` (`fake_console`, `api_client`, `mock_client`, `mock_listener_run`, `setup_entry`)

**Agent G: Docs & user contract.** For every change, is each affected claim still true? Check:
- **README:** prerequisites (dedicated local-only Super Admin without MFA, and why), the plaintext-credential security note, HACS install and beta visibility, setup steps, "Behaviour worth knowing", the troubleshooting table, the undocumented-API disclaimer.
- **`translations/en.json` text:** does each message describe what the code actually does?
- **The spec:** §3 "Verified on the real console" and "Not yet verified" (move items when behaviour is verified or changes), and §4/§5 when behaviour changes.
- **CLAUDE.md** (architecture summary and rules) and **CONTRIBUTING.md** (commands, release convention).

Also flag:
- a new user-visible behaviour, repair, option or error with no doc or translation;
- a doc that promises something the code doesn't do.

**Agent H: Slop, simplification & house style.** At most 2 simplification recommendations, plus uncapped outright slop. "Nothing found" is a valid result. Hunt for:
- speculative parameters or abstractions with a single use;
- defensive handling of states that can't occur;
- scratch or debug files in the diff;
- comments that narrate the task or the debugging instead of explaining why.

House style comes from `pyproject.toml` (ruff, 88 columns, isort with force-sort-within-sections) and the existing code:
- typed functions;
- `from __future__ import annotations`;
- dataclasses for API models;
- typed errors from `api.py`, never bare exceptions;
- constants in `const.py`;
- user-facing text only in `translations/en.json`;
- short docstrings, and comments that say why.

Python 3.14 syntax such as unparenthesised `except A, B:` is ruff's normalisation, not slop. Flag divergences with a concrete sibling reference (`file:line`). Never recommend a change solely for consistency or line count.

**Agent I: Release, packaging & CI.** Hunt for:
- **`release.yml`.**
  - It triggers only on `v*` tags.
  - It stamps `manifest.json` `version` from the tag, and the version must be valid for HA.
  - It zips the *contents* of `custom_components/unifi_protect_alarm_bridge/` so they sit at the zip root, excluding `__pycache__`.
  - The asset name must equal `hacs.json` `filename`.
  - Tags with a `-suffix` must be marked as pre-releases.
  - Permissions must be minimal.
- **`validate.yml`.** hassfest, `hacs/action` (category `integration`, `ignore: brands` justified), ruff, and pytest must stay wired to `requirements_test.txt`.
- **`hacs.json`.** `homeassistant` minimum, `zip_release`, `filename`, `render_readme`, `content_in_root`.
- **`requirements_test.txt` pins.** Keep them consistent with the supported HA version.
- **`pyproject.toml`.** pytest/ruff config, and the `extend-exclude = ["docs"]` that stops ruff rewriting Markdown code blocks.
- **Workflow security.** Use `uvx zizmor .github` results: unpinned third-party actions, excessive `permissions`, script injection via `${{ github.* }}` in `run:`.
- **Local harness.** `scripts/develop` must be safe and repeatable (the symlink target, the pinned HA version).
- **`.gitignore` coverage.**

Never tag, release or push while reviewing.
