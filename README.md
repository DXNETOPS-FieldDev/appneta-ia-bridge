# appneta-ia-bridge

`appneta-ia-bridge` receives AppNeta Intelligent Alarms webhook events (`ALARM_RAISED` and
`ALARM_CLEARED`) and forwards them as SNMP v2c traps to Broadcom DX NetOps Spectrum, Smarts, or any
other SNMP trap receiver. Alarms raised in AppNeta appear in your operations console and clear
there when AppNeta clears them.

It is a small Python service with no third-party dependencies, packaged with an nginx TLS front end
in a single Docker image. Companion install bundles configure the Spectrum and Smarts side.

- **Version:** 0.2.2
- **Install:** [`INSTALLATION.md`](INSTALLATION.md)
- **Changes:** [`CHANGELOG.md`](CHANGELOG.md)

## How it works

```
AppNeta cloud ──HTTPS webhook──▶ nginx (TLS, :443) ──▶ alarm_bridge (:8080, in-container)
                                                              │
                                                    SNMP v2c trap (UDP)
                                                              ▼
                                     Spectrum / Smarts / other trap receiver
```

1. An AppNeta alarm connector posts each alarm event to the bridge over HTTPS.
2. The bridge authenticates the request, validates the payload, and maps it to an SNMP trap.
3. The trap is sent to every configured destination. The destination-side install bundle
   (`spectrum-install/`, `spectrum-install-general/`, `smarts-install/`) turns the trap into an
   alarm or event and clears it when the matching `ALARM_CLEARED` trap arrives.

## Requirements

- Docker on a host reachable from the internet on `443/tcp` (AppNeta delivers webhooks from its
  cloud).
- A DNS name for that host and a TLS certificate for it from a publicly trusted certificate
  authority (see [TLS](#tls)).
- A Spectrum or Smarts instance (or other trap receiver) that can receive UDP traps from the host.
- An AppNeta organization with permission to create an alarm connector.

## Configuration

The container is configured with environment variables.

Trap destinations are numbered from 1. The bridge sends every alarm to each destination and stops
reading at the first number that has no `_HOST` set. At least `TRAP_DESTINATION_1_HOST` is
required.

| Variable | Required | Default | Description |
|---|---|---|---|
| `TRAP_DESTINATION_<N>_HOST` | `N=1` | — | Hostname or IP of trap destination `N`. |
| `TRAP_DESTINATION_<N>_PORT` | no | `162` | UDP port of destination `N`. |
| `TRAP_DESTINATION_<N>_COMMUNITY` | no | `public` | SNMP community string; must match the receiver. |
| `TRAP_DESTINATION_<N>_SCHEME` | yes | — | `vna` or `general`. Must match the mode installed on destination `N` (`intelligent_alarms.sh status`). Smarts always uses `general`. The container refuses to start if it is unset or invalid. |
| `TRAP_DESTINATION_<N>_SOURCE_ADDRESS` | yes | — | Dotted IPv4 address sent in every trap to destination `N` as `snmpTrapAddress.0` (OID `1.3.6.1.6.3.18.1.3.0`), telling the receiver which device the trap is about when NAT or a container hides the real sender address. For Spectrum use the IP address of the AppNeta model; for Smarts, the element's IP address. `none` (any case) sends no claim, as in v0.1.1 and v0.1.2. Do not use the IP address of another real device: its alarms would be routed to that device. The container refuses to start if it is unset or invalid. |
| `AUTH_TYPE` | yes | — | `NONE`, `BASIC` or `BEARER`. Must match the authentication configured on the AppNeta connector. |
| `AUTH_TOKEN` / `AUTH_TOKEN_FILE` | `AUTH_TYPE=BEARER` | — | Bearer token, or the path of a file containing it. |
| `AUTH_USERNAME` | `AUTH_TYPE=BASIC` | — | Basic-auth user name. |
| `AUTH_PASSWORD` / `AUTH_PASSWORD_FILE` | `AUTH_TYPE=BASIC` | — | Basic-auth password, or the path of a file containing it. |
| `APPNETA_NIS_SERVER` | a `vna` destination | — | Label of your AppNeta platform host, for example `app-12` for `https://app-12.pm.appneta.com`. Selects the ID offset from the table in `alarm_bridge/config.py`. |
| `APPNETA_V3_ID_OFFSET` | a `vna` destination | — | Numeric v4-to-v3 ID offset for your AppNeta platform host. Use it when your host is not in the table; it takes precedence over `APPNETA_NIS_SERVER`. |
| `TLS_ENABLED` | no | `true` | `false` serves plain HTTP on `:443`. See [TLS](#tls). |
| `LISTEN_PORT` | no | `8080` | Port `alarm_bridge` listens on inside the container, behind nginx. Not normally changed. |

A `vna` destination needs `APPNETA_NIS_SERVER` or `APPNETA_V3_ID_OFFSET`: Spectrum models built
from AppNeta's earlier integration identify paths by their 32-bit (v3 API) IDs, so the bridge
sends the v3 equivalent (`v3_id = v4_id - offset`) alongside each 64-bit ID. `general` destinations
need neither.

The bridge's command line also accepts a repeatable
`--trap-destination HOST:PORT:COMMUNITY[:SCHEME[:SOURCE_ADDRESS]]` flag. When any is given, they replace the
`TRAP_DESTINATION_*` environment variables entirely. Leaving out the fifth field sends no claim
(the same as `none`).

Prefer `AUTH_TYPE=BEARER` with a long random token. `AUTH_TYPE=NONE` lets anyone who can reach the
endpoint send alarms to your trap receiver, including clearing real ones.

## TLS

nginx terminates TLS inside the container. You supply the certificate and key and mount them
read-only at:

```
/etc/appneta-ia-bridge/tls/cert.pem    # certificate, followed by any intermediate certificates
/etc/appneta-ia-bridge/tls/key.pem     # private key
```

The container does not start if either file is missing. Use a certificate from a publicly trusted
CA whose subject alternative names include the DNS name in the connector URL; AppNeta can then
verify it with the connector's `ignoreSSLErrors` setting left `false`. A self-signed certificate
also works if the connector sets `ignoreSSLErrors` to `true`; see
[`INSTALLATION.md`](INSTALLATION.md#appendix-self-signed-certificate).

`TLS_ENABLED=false` serves plain HTTP on the same port. The authentication secret then crosses the
network unencrypted, so use it only for testing; the container logs a warning at every start while
it is set.

nginx always listens on `443` inside the container; use Docker's `-p` option to publish a
different external port.

## Security summary

For teams reviewing the code before deployment. The service is about 700 lines of standard-library
Python in `alarm_bridge/`.

| Area | Behavior | Where |
|---|---|---|
| Transport | TLS 1.2 and 1.3 terminated by nginx; certificate and key are supplied by you and never baked into the image. | `docker/nginx-tls.conf` |
| Authentication | `NONE`, `BASIC` or `BEARER`, chosen explicitly (no default). Credentials are compared in constant time. The request body is not read until authentication succeeds. | `alarm_bridge/auth.py`, `alarm_bridge/server.py` |
| Request limits | Bodies over 1 MB are rejected (`413`); a missing, negative or non-numeric `Content-Length` is rejected (`400`); each connection has a 15 s timeout. nginx enforces the same 1 MB cap. | `alarm_bridge/server.py`, `docker/nginx-*.conf` |
| Input validation | The payload must be a JSON object; severity, state, `orgId` and `ruleId` are validated; invalid input returns `400` and never reaches the trap sender. If every trap send fails the bridge returns `502`. | `alarm_bridge/mapping.py` |
| Secrets | `AUTH_TOKEN_FILE` / `AUTH_PASSWORD_FILE` keep secrets out of `docker inspect` and shell history. The bridge does not log credentials. | `docker/entrypoint.sh`, `alarm_bridge/config.py` |
| Privileges | The entrypoint runs as root only to read secret files and let nginx bind `:443`; `alarm_bridge` runs as the unprivileged `appuser`. | `Dockerfile`, `docker/entrypoint.sh` |
| Outbound traffic | Only SNMP v2c traps over UDP to the configured destinations. The bridge makes no other outbound connections and has no third-party dependencies. | `alarm_bridge/snmp_trap.py` |
| Install scripts | The Spectrum and Smarts installers append marked blocks to existing configuration files, record a checksum, and refuse to uninstall if the files were edited since. | `lib/`, `intelligent_alarms.sh` |

Tests that exercise these behaviors are in `tests/`; run them with
`python3 -m unittest discover -s tests`.

## Maintenance

- **Logs:** `docker logs -f appneta-ia-bridge`
- **Change configuration:** recreate the container (`docker rm -f appneta-ia-bridge`, then `docker
  run` again with the new settings); `docker restart` reuses the old settings.
- **Upgrade:** `git fetch --tags && git checkout <new tag>`, rebuild the image, recreate the container. Re-run the destination-side
  installer if `CHANGELOG.md` says the bundles changed.
- **Uninstall:** `docker rm -f appneta-ia-bridge`. Remove the destination-side bundles with
  `intelligent_alarms.sh uninstall`.
- **Secrets at rest:** keep tokens in files mounted read-only and referenced with `*_FILE`, as shown
  in [`INSTALLATION.md`](INSTALLATION.md).

## Support

Contact your Broadcom representative for help with `appneta-ia-bridge`. For AppNeta connector
configuration, see AppNeta's documentation or support channel.

## License

See [`LICENSE`](LICENSE).
