# Changelog

## v0.2.0

Split Spectrum installs, MIB compile and install logging.

- **CLI change:** `intelligent_alarms.sh --spectrum` is replaced by `--spectroserver` (the single-host
  install, or the SpectroSERVER host of a split install). `--spectrum` now exits with an error naming the
  new flags. `--smarts` and `status` are unchanged.
- Split Spectrum installs (SpectroSERVER and OneClick on separate hosts): new
  `intelligent_alarms.sh --oneclick [--vna|--general] install|uninstall|status` for the
  OneClick host, which installs only the alarm text files OneClick reads from its own host. See
  `INSTALLATION.md`.
- The Spectrum installers now compile the MIBs themselves (both `.mib` files, via `BulkMibCompile.sh`)
  and exit `1` if a MIB fails to compile. The compile is skipped on a host with no `MibDatabase`.
- `intelligent_alarms.sh install` and `uninstall` now write `logs/<yyyymmddhhmm>-install.log` or `-uninstall.log` (created if missing;
  `IA_LOG_DIR` overrides the location) with the command, user, host, all output and the exit status.
- Uninstall is safer: both `AlertMap` and `EventDisp` are checked before either is changed, the files keep
  their owner and mode, a damaged state marker produces an error instead of a silent exit 1, and `status`
  reports an unreadable marker instead of "not installed". Bare `status` also shows a OneClick-host install.
- The bridge now answers `502` (was `400`) when it tried to send a trap and every destination failed, so a
  webhook sender that retries on 5xx retries a transient network error. A payload that is rejected still
  returns `400`.
- `status` reports "INCOMPLETE install" when an install stopped before finishing (for example a MIB compile
  failure), until `install` is re-run successfully.
- `--spectroserver install` prints a reminder to run `--oneclick install` on the OneClick host when this host has no
  OneClick (`tomcat/`).
- `--oneclick` is refused on a host that has a SpectroSERVER (`SS/`); use `--spectroserver` there.
- Fixed `--spectroserver --general uninstall` leaving `Event06732179_en_US` and `Event0673217b_en_US`
  behind.

## v0.1.3

Sender identity in traps.

- Every trap can now carry `snmpTrapAddress.0` (OID `1.3.6.1.6.3.18.1.3.0`, IpAddress). NAT and
  containers hide the real sender address from Spectrum and Smarts; this tells them which device
  the trap is about.
- New required setting `TRAP_DESTINATION_<N>_SOURCE_ADDRESS` for each destination: a dotted IPv4
  address, or `none` to send no claim. The `--trap-destination` flag takes it as an optional fifth
  field.
- Upgrading from v0.1.1 or v0.1.2: add `-e TRAP_DESTINATION_<N>_SOURCE_ADDRESS=<IP>` (N = each
  destination's number), or `none` to keep the old behavior. Without it the container exits at startup with
  a message naming the variable.
- Do not use the IP address of another real device; its alarms would be routed to that device.

## v0.1.2

Documentation update.

- `INSTALLATION.md` now lists `git` as a prerequisite for step 1.

## v0.1.1

License and configuration-message updates.

- The software is now released under the MIT License.
- Clearer configuration error messages for the `vna` scheme.

## v0.1.0

Initial release.

- Webhook receiver for AppNeta Intelligent Alarms (`ALARM_RAISED`, `ALARM_CLEARED`) that sends SNMP
  v2c traps to one or more destinations.
- Spectrum integration with two modes: `general` (alarm per Critical/Major incident) and `vna`
  (alarms on existing `NetworkPath`/`WebPath` models).
- Smarts integration (`general` mode).
- Bundled nginx TLS termination with certificates supplied by you; support for `ignoreSSLErrors`
  set to `false` on the AppNeta connector with a publicly trusted certificate.
- Bearer-token, basic and no authentication, with secrets read from files.
- Request-size and timeout limits and input validation on the webhook.
- Install, uninstall and status script for the Spectrum and Smarts bundles, with backout steps.
