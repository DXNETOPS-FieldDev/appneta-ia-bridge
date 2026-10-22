# Changelog

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
