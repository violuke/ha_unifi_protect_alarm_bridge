# UniFi Protect Alarm Bridge

A Home Assistant alarm panel for UniFi Protect's **Global Alarm Manager**.

Protect switches Alarm Manager to Global mode as soon as sensors, relays, fobs or
an Alarm Hub are adopted. In Global mode the public Protect API cannot arm,
disarm or read the arm state, so the official UniFi Protect integration cannot
offer an alarm panel. This integration talks to Alarm Manager directly:

- **State** comes from Alarm Manager's arm profiles. Dedicated-account mode uses
  websocket updates plus polling every 60 seconds while connected or 10 seconds
  while disconnected. Session reuse polls every 15 seconds.
- **Arm and disarm** call Alarm Manager's own profile actions.
- The panel shows `disarmed`, `arming` (during the exit delay), `armed_away`,
  `armed_home` or `armed_night`, and `triggered`. It never assumes a state it
  hasn't read from the console.

Dedicated-account mode runs independently. Optional session reuse depends on the
official UniFi Protect integration remaining loaded.

> **Unofficial.** This uses undocumented UniFi OS APIs, which a firmware update
> could change. If that happens you'll get a repair notice in Home Assistant
> rather than a silently wrong alarm state.

## Prerequisites

1. **Protect's Alarm Manager in Global mode, with at least one arm profile.**
2. **An existing full-access Protect connection with sufficient alarm permissions,
   or a dedicated local Super Admin account.** For a dedicated account, use
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

1. In HACS, search for **UniFi Protect Alarm Bridge**.
2. Download it, then restart Home Assistant.

## Setup

1. Go to **Settings → Devices & services → Add integration → UniFi Protect
   Alarm Bridge**.
2. If offered, choose an existing Protect connection (see below) or a dedicated
   account. For a dedicated account, enter the console address and Super Admin
   username and password. Leave **Verify SSL certificate** off unless the
   console has a certificate your Home Assistant trusts.
3. If you have more than one arm profile, choose which profile **Arm away**
   uses. **Arm home** and **Arm night** are optional.

You can change the mapping later under the integration's **Configure** button.

### Reuse an existing Protect connection

If the official UniFi Protect integration is configured, setup offers a choice:
reuse its authenticated connection or keep using a dedicated account. Existing
dedicated-account entries and their TLS preferences are unchanged.

Session reuse requires a loaded full-access Protect connection whose account can
access Global Alarm Manager; API-key-only connections do not provide this session.
It does not copy the stored password, log in or out, or close the source session.
The official integration remains responsible for authentication and must stay enabled.

This mode polls every 15 seconds and opens no additional websocket. It validates
the HTTPS certificate independently; for a self-signed console, enter its
independently verified SHA-256 fingerprint. Failed writes are not retried.

### Behaviour worth knowing

- **Arming from HA respects the profile's exit delay.** The panel shows
  `arming` until Protect reports `armed`.
- **Arming one mode disarms any other active profile first.**
- **Disarm disarms every active profile.**
- **Arming from the Protect app, a schedule or a fob shows up within about a
  second in dedicated-account mode, or on the next poll (normally within
  15 seconds) when reusing a connection.** If a profile you haven't mapped is armed, the panel shows
  `armed_custom_bypass`, and its `profile_title` attribute names the profile.
- **If the official UniFi Protect integration is also installed, the alarm
  panel appears on the same console device** (both integrations identify it
  by its MAC address).

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
