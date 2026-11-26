# appneta-ia-bridge

`appneta-ia-bridge` receives AppNeta Intelligent Alarms webhook events (`ALARM_RAISED` and
`ALARM_CLEARED`) and forwards them as SNMP v2c traps to Broadcom DX NetOps Spectrum, Smarts, or any
other SNMP trap receiver. Alarms raised in AppNeta appear in your operations console and clear
there when AppNeta clears them.

It is a small Python service with no third-party dependencies, packaged with an nginx TLS front end
in a single Docker image. Companion install bundles configure the Spectrum and Smarts side.

**Version:** 0.4.0

## Documentation

| I want to... | Read |
|---|---|
| Install the bridge and connect AppNeta to Spectrum or Smarts | [`INSTALLATION.md`](INSTALLATION.md), step by step |
| Look up or change a setting, read a log line, understand spooling | [Configuration](#configuration) below, and [`REFERENCE.md`](REFERENCE.md) |
| Send metrics to Prometheus and Grafana | [Metrics](#metrics) below, and [`REFERENCE.md`](REFERENCE.md#metrics-for-prometheus-and-grafana) |
| Run, upgrade, back up or roll back the bridge | [Operate and maintain](#operate-and-maintain) below |
| See what changed in a release, and whether an upgrade needs extra steps | [`CHANGELOG.md`](CHANGELOG.md) |
| Review the security design | [Security summary](#security-summary) below, and [`SECURITY.md`](SECURITY.md) |
| Install the Spectrum or Smarts side by hand, or back it out | [`spectrum-install/README.md`](spectrum-install/README.md) (both Spectrum modes) and [`smarts-install/README.md`](smarts-install/README.md) |

## How it works

```
AppNeta cloud ──HTTPS webhook──▶ nginx (TLS, :8443 -> published as 443) ──▶ alarm_bridge (:8080, in-container)
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
- A DNS name for that host and a TLS certificate for it, from a CA or self-signed (see [TLS](#tls)).
- A Spectrum or Smarts instance (or other trap receiver) that can receive UDP traps from the host.
- An AppNeta organization with permission to create an alarm connector.

## Quick start

The full procedure, with every command, is in [`INSTALLATION.md`](INSTALLATION.md). In outline:

1. **Build the image** on a Docker host: clone this repository, check out the release tag, and run
   `docker build -t appneta-ia-bridge .`
2. **Install your TLS certificate** and key in `/etc/appneta-ia-bridge/tls/` (certificate first, then
   intermediates). The host needs a DNS name and `443/tcp` open to the internet.
3. **Create the webhook token** in a root-owned file under `/etc/appneta-ia-bridge/secrets/`. You will
   paste the same value into the AppNeta connector.
4. **Run the container** with one `TRAP_DESTINATION_<N>_*` group of settings for each receiver, the
   authentication settings, and a named volume for the alarm spool.
5. **Install the destination side** on your Spectrum or Smarts server with `intelligent_alarms.sh`, in the
   same mode (`vna` or `general`) as the container's `TRAP_DESTINATION_<N>_SCHEME`.
6. **Create the AppNeta alarm connector** pointing at `https://<your-host>/` with the same token.
7. **Verify**: within a minute of a real alarm, `docker logs appneta-ia-bridge` shows a summary line with
   `received` and `delivered` above zero, and the alarm appears in Spectrum or Smarts.

## Configuration

The container is configured with environment variables.

Trap destinations are numbered from 1. The bridge sends every alarm to each destination and stops
reading at the first number that has no `_HOST` set. At least `TRAP_DESTINATION_1_HOST` is
required.

| Variable | Required | Default | Description |
|---|---|---|---|
| `TRAP_DESTINATION_<N>_HOST` | `N=1` | — | Hostname or IP of the server that receives trap destination `N`'s traps: the SpectroSERVER host for Spectrum (not OneClick), the `sm_trapd` host for Smarts. |
| `TRAP_DESTINATION_<N>_PORT` | no | `162` | UDP port of destination `N`. |
| `TRAP_DESTINATION_<N>_COMMUNITY` / `_COMMUNITY_FILE` | **yes, no default** | — | SNMP community string; must match the receiver. `_FILE` (a mounted secret) takes precedence. |
| `TRAP_DESTINATION_<N>_SCHEME` | yes | — | `vna` or `general`. Must match the mode installed on destination `N` (`intelligent_alarms.sh status`). Smarts always uses `general`. The container refuses to start if it is unset or invalid. |
| `TRAP_DESTINATION_<N>_SOURCE_ADDRESS` | yes | — | Dotted IPv4 address sent in every trap to destination `N` as `snmpTrapAddress.0` (OID `1.3.6.1.6.3.18.1.3.0`), telling the receiver which device the trap is about when NAT or a container hides the real sender address. For Spectrum use the IP address of the AppNeta model; for Smarts, the element's IP address. `none` (any case) sends no claim, as in v0.1.1 and v0.1.2. Do not use the IP address of another real device: its alarms would be routed to that device. The container refuses to start if it is unset or invalid. |
| `AUTH_TYPE` | yes | — | `BEARER`, `BASIC` or `HEADER`. Must match the authentication configured on the AppNeta connector. There is no unauthenticated mode; the container refuses to start without one of these. `HEADER` checks the request header named by `AUTH_HEADER_NAME` against `AUTH_TOKEN`. |
| `AUTH_HEADER_NAME` | if `AUTH_TYPE=HEADER` | — | Header that carries the token: letters, digits and hyphens only, 1 to 64 characters. |
| `AUTH_TOKEN` / `AUTH_TOKEN_FILE` | `AUTH_TYPE=BEARER` | — | Bearer token, or the path of a file containing it. |
| `AUTH_USERNAME` | `AUTH_TYPE=BASIC` | — | Basic-auth user name. |
| `AUTH_PASSWORD` / `AUTH_PASSWORD_FILE` | `AUTH_TYPE=BASIC` | — | Basic-auth password, or the path of a file containing it. |
| `METRICS_AUTH_TOKEN` / `METRICS_AUTH_TOKEN_FILE` | no | — | Read-only bearer token for `/metrics`. Setting it is what turns `/metrics` on; unset, the endpoint does not exist (404). It opens nothing else and the webhook credentials never open `/metrics`. Must differ from `AUTH_TOKEN`. |
| `APPNETA_NIS_SERVER` | a `vna` destination | — | Label of your AppNeta platform host, for example `app-12` for `https://app-12.pm.appneta.com`. Selects the ID offset from the table in `alarm_bridge/config.py`. |
| `APPNETA_V3_ID_OFFSET` | a `vna` destination | — | Numeric v4-to-v3 ID offset for your AppNeta platform host. Use it when your host is not in the table; it takes precedence over `APPNETA_NIS_SERVER`. |
| `TLS_ENABLED` | no | `true` | `false` serves plain HTTP (inside the container on `:8443`, published as 443). See [TLS](#tls). |
| `LISTEN_HOST` | no | `127.0.0.1` in the image | Address `alarm_bridge` binds to inside the container. Loopback means only nginx can reach it. Not normally changed. |
| `LISTEN_PORT` | no | `8080` | Port `alarm_bridge` listens on inside the container, behind nginx. Not normally changed. |

Duplicate-suppression, alarm-spool, destination-health and logging settings (`DEDUP_*`, `SPOOL_*`, `DEST_*`, `LOG_*`, `TRAP_DESTINATION_<N>_NAME` and `_PROBE_TCP_PORT`) are described in [`REFERENCE.md`](REFERENCE.md#extended-settings).

A `vna` destination needs `APPNETA_NIS_SERVER` or `APPNETA_V3_ID_OFFSET`: Spectrum models built
from AppNeta's earlier integration identify paths by their 32-bit (v3 API) IDs, so the bridge
sends the v3 equivalent (`v3_id = v4_id - offset`) alongside each 64-bit ID. `general` destinations
need neither.

The bridge's command line also accepts a repeatable
`--trap-destination HOST:PORT:COMMUNITY[:SCHEME[:SOURCE_ADDRESS]]` flag. When any is given, they replace the
`TRAP_DESTINATION_*` environment variables entirely. Leaving out the fifth field sends no claim
(the same as `none`).

Prefer `AUTH_TYPE=BEARER` with a long random token. There is no unauthenticated mode: an open
endpoint would let anyone who can reach it send alarms to your trap receiver, including clearing real
ones. In AppNeta, `auth.type` accepts only `BASIC` or `BEARER`; for `HEADER`, add the header and token
to the connector's `headers` list.

## Metrics

Prometheus and Grafana can scrape the bridge. `/metrics` is off until you create a read-only token for it, and
the scraper never holds the webhook secret:

```bash
openssl rand -hex 32 | sudo tee /etc/appneta-ia-bridge/secrets/metrics_token >/dev/null
sudo chgrp 10001 /etc/appneta-ia-bridge/secrets/metrics_token
sudo chmod 640 /etc/appneta-ia-bridge/secrets/metrics_token
```

Add this setting to the `docker run` command (the secrets directory is already mounted at `/run/secrets`):

```bash
  -e METRICS_AUTH_TOKEN_FILE=/run/secrets/metrics_token \
```

Check it without putting the token on a command line, then copy the file to the Prometheus host
(`authorization.credentials_file`):

```bash
sudo sh -c 'printf "Authorization: Bearer %s\n" "$(cat /etc/appneta-ia-bridge/secrets/metrics_token)"' \
  | curl --cacert /etc/appneta-ia-bridge/tls/cert.pem -H @- https://bridge.example.com/metrics
```

The series, a scrape job and suggested alerts are in `INSTALLATION.md`.

## TLS

nginx terminates TLS inside the container. You supply the certificate and key and mount them
read-only at:

```
/etc/appneta-ia-bridge/tls/cert.pem    # certificate, followed by any intermediate certificates
/etc/appneta-ia-bridge/tls/key.pem     # private key
```

The container does not start if either file is missing. The certificate's subject alternative names must include the
host in the connector URL (the DNS name, or the IP address if the URL uses one), even for a self-signed
certificate. A certificate from a CA that AppNeta's servers trust lets AppNeta verify
the bridge, with the connector's `ignoreSSLErrors` left `false`. A self-signed certificate also works if
the connector sets `ignoreSSLErrors` to `true`; see
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
| Authentication | `BEARER`, `BASIC` or `HEADER`, chosen explicitly (no default, no unauthenticated mode). Credentials are compared in constant time. The request body is not read until authentication succeeds. | `alarm_bridge/auth.py`, `alarm_bridge/server.py` |
| Request limits | Bodies over 1 MB are rejected (`413`); a missing, negative or non-numeric `Content-Length` is rejected (`400`); each connection has a 15 s timeout. nginx enforces the same 1 MB cap. | `alarm_bridge/server.py`, `docker/nginx-*.conf` |
| Input validation | The payload must be a JSON object; severity, state, `orgId` and `ruleId` are validated; invalid input returns `400` and never reaches the trap sender. With spooling on (the default) an alarm for a down destination is stored and answered `200`, and `503` is returned only when the spool cannot store it; with spooling off (`DEDUP_STATE_DIR` empty) a failure of every send returns `502`. | `alarm_bridge/mapping.py` |
| Secrets | `AUTH_TOKEN_FILE` / `AUTH_PASSWORD_FILE` keep secrets out of `docker inspect` and shell history. The bridge does not log credentials. | `docker/entrypoint.sh`, `alarm_bridge/config.py` |
| Privileges | The whole container runs as the unprivileged uid 10001 (never root); nginx listens on `:8443`, published as 443. `/app` is root-owned and read-only to it. Mounted secrets are group-readable (`root:10001`, `640`). | `Dockerfile`, `docker/entrypoint.sh` |
| Outbound traffic | Only SNMP v2c traps over UDP to the configured destinations. The bridge makes no other outbound connections and has no third-party dependencies. | `alarm_bridge/snmp_trap.py` |
| Install scripts | The Spectrum and Smarts installers append marked blocks to existing configuration files, record a checksum, and refuse to uninstall if the files were edited since. | `lib/`, `intelligent_alarms.sh` |

Tests that exercise these behaviors are in `tests/`; run them with
`python3 -m unittest discover -s tests`.

## Operate and maintain

**Check health.**

- `docker ps` shows `(healthy)` next to the container. Docker checks it every 30 seconds.
- `docker logs -f appneta-ia-bridge` shows a summary line each minute that traffic arrives:
  `received` (authenticated requests), `delivered` (sent as a trap to at least one destination),
  `duplicates`, `rejected`, `auth_failed` and `send_failed`. No summary line means nothing is reaching
  the bridge.
- With a [metrics token](#metrics), Prometheus can alert on `destination_up == 0`, a growing spool, and
  no webhooks received. [`REFERENCE.md`](REFERENCE.md#metrics-for-prometheus-and-grafana) lists the series.

**Change configuration.** Recreate the container: `docker rm -f appneta-ia-bridge`, then `docker run`
again with the new settings. `docker restart` reuses the old settings. The alarm spool and duplicate
history live in the `ia-bridge-state` volume, so they survive this.

**Rotate the webhook token.** Write the new value to the token file, update the AppNeta connector, then
`docker restart appneta-ia-bridge`.

**Back up.** The bridge keeps no configuration state of its own. Keep a copy of your `docker run`
command (or compose file), the certificate and key, and the secrets files. The `ia-bridge-state`
volume holds queued alarms and duplicate history; it is worth keeping across upgrades but not worth
restoring from backup.

**Upgrade.**

1. Read [`CHANGELOG.md`](CHANGELOG.md) for every release between your version and the new one. It says
   whether the destination-side bundles or any setting changed.
2. Keep the running image so you can go back: `docker tag appneta-ia-bridge appneta-ia-bridge:previous`.
3. `git fetch --tags && git checkout <new tag>`, then `docker build -t appneta-ia-bridge .`
4. Recreate the container with your existing `docker run` command (the volume is reused).
5. If the changelog lists destination-side changes, run `./intelligent_alarms.sh --spectroserver uninstall`
   (or `--smarts`) and install again from the new version.
6. Confirm `docker ps` shows `(healthy)` and a summary line with `delivered` above zero after the next alarm.

**Roll back.** `docker rm -f appneta-ia-bridge`, then repeat your `docker run` command with the image
`appneta-ia-bridge:previous` in place of `appneta-ia-bridge`. If you re-ran the destination-side
installer during the upgrade, install the older version's bundle again.

**Uninstall.** `docker rm -f appneta-ia-bridge`. Remove the destination-side bundles with
`./intelligent_alarms.sh uninstall`, and delete the volume with `docker volume rm ia-bridge-state` if
you no longer need it. The Spectrum rollback levels are in
[`spectrum-install/README.md`](spectrum-install/README.md#backout-plan).

**Keep secrets in files.** Mount tokens read-only and reference them with `*_FILE`, as shown in
[`INSTALLATION.md`](INSTALLATION.md).

## Support

Contact your Broadcom representative for help with `appneta-ia-bridge`. For AppNeta connector
configuration, see AppNeta's documentation or support channel.

## License

See [`LICENSE`](LICENSE).
