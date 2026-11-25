# Smarts install bundle

Configures an existing VMware/Telco Cloud Smarts instance to turn `appneta-ia-bridge` traps into
notifications. Smarts uses the `general` (severity-keyed) mode only: 8 trap OIDs, `.126`–`.133`
under `apnNotifBranch`, raised and cleared for each severity. Set the bridge destination's
`TRAP_DESTINATION_<N>_SCHEME` to `general`.

## Prerequisites

1. **`INCHARGE-OI` must be installed and running.** It carries the `SNMPTrapIndication` /
   `ICS_Notification` model these traps need. Without it the install succeeds but no events are
   created.
   ```bash
   dmctl -s INCHARGE-OI ping
   # if that fails:
   sm_service install ic-oi-server
   sm_service start ic-oi-server
   ```
   Run interactively, `dmctl` prompts for a Smarts login and defaults the user name to your OS
   user. Smarts authenticates against its own accounts, so log in as the Smarts administrator
   account. A rejected login (`SM-E-ELOGIN ... permission denied`) is a credentials problem, not a
   sign that `INCHARGE-OI` is down.
2. **IPv6.** If this Smarts instance runs with `com.smarts.ip_versions=v6v4`, a global IPv6 address on
   the host can interfere with the trap adapter's reconnect behavior. The installer warns but does
   not stop. If traps stop producing events after install, check `ip -6 addr show scope global`.

## Install, uninstall, status

Use the dispatcher at the top of the repository, from the directory you copied the bundle to
(`intelligent_alarms.sh`, `lib/` and `smarts-install/` side by side):

```bash
./intelligent_alarms.sh --smarts --general install
./intelligent_alarms.sh --smarts uninstall
./intelligent_alarms.sh --smarts status
./intelligent_alarms.sh status                  # Spectrum and Smarts
```

`SMARTS_HOME` defaults to `/opt/InCharge/SAM/smarts`; set it if yours is elsewhere.

`install` is idempotent. It appends one marked block to `conf/icoi/trap_mgr.conf` and records the
mode, line count and SHA-256 of that block in `conf/icoi/.intelligent-alarms-state`. `uninstall`
checks the checksum first and refuses, changing nothing, if the file was edited or appended to
after the install.

## Verify delivery

Verify after every install; a successful install does not prove traps are being turned into events.

1. Send one test alarm per severity through the bridge (see step 7 of
   [`INSTALLATION.md`](../INSTALLATION.md#7-verify)) with a `general` destination pointing at this
   Smarts host. Use `CRITICAL`, `MAJOR`, `MINOR` and `WARNING`.
2. Confirm a notification exists for each:
   ```bash
   dmctl -s INCHARGE-OI getInstances ICS_Notification
   ```
3. Send the matching `CLEARED` alarm and confirm the instance is gone, or shows `Active = FALSE`
   and `EventState = INACTIVE`.

Severity mapping: `CRITICAL` = 1, `MAJOR` = 2, `MINOR` = 3. `WARNING` is also shown as `MINOR`
severity (3) but keeps its own event name.

No automatic expiry is configured. If a `CLEARED` trap is lost, clear the notification by hand:

```bash
dmctl -s INCHARGE-OI invoke ICS_Notification::NOTIFICATION-SNMPTrapIndication_<itemName>_<EventName> \
    clear <user> 'Trap Processor' '<audit text>' 0
```

## Bridge side

Add a `TRAP_DESTINATION_<N>_*` set to the bridge with `_SCHEME=general` for this host. Set
`TRAP_DESTINATION_<N>_SOURCE_ADDRESS` to the IP address of the host element in Smarts that the
alarms should attach to. See [`README.md`](../README.md#configuration).
