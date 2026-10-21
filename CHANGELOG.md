# Changelog

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
