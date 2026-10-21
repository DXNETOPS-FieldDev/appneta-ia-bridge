# Installation

This guide installs the bridge container, connects an AppNeta alarm connector to it, and installs
the matching configuration on your Spectrum or Smarts server. Allow about an hour. See
[`README.md`](README.md) for the full configuration reference.

## Before you start

You need:

- A Linux host with Docker and `git` installed, reachable from the internet on `443/tcp`.
- A DNS name for that host (examples below use `ia-bridge.example.com`).
- A TLS certificate and private key for that name from a publicly trusted certificate authority.
  The certificate file must contain the full chain (your certificate followed by the
  intermediates).
- Network access from that host to your trap receiver on UDP `162` (or your configured port).
- An AppNeta account that can create an alarm connector.
- For Spectrum or Smarts: SSH access to the server and the account that owns the installation.

Decide which destination type you have. It sets the `TRAP_DESTINATION_<N>_SCHEME` value in step 4
and the installer you run in step 5.

| Destination | Scheme | Install |
|---|---|---|
| Spectrum **with** the earlier AppNeta (VNA) integration modeling `NetworkPath`/`WebPath` | `vna` | [Spectrum](#spectrum) |
| Spectrum **without** it (the common case) | `general` | [Spectrum](#spectrum) |
| Smarts | `general` | [Smarts](#smarts) |
| Any other SNMP trap receiver | `general` | none; see [Other receivers](#other-snmp-trap-receivers) |

Not sure whether your Spectrum has the VNA integration? Look for `NetworkPath` or `WebPath` models
that carry an `AppNetaPathIdentifier` attribute (`0x673009f`), or ask your Spectrum administrator.

## 1. Get the code and build the image

```bash
git clone https://github.com/DXNETOPS-FieldDev/appneta-ia-bridge.git appneta-ia-bridge
cd appneta-ia-bridge
git checkout v0.1.2
docker build -t appneta-ia-bridge .
```

To review the code first, start with the [security summary in `README.md`](README.md#security-summary).

## 2. Install the TLS certificate

Place the certificate chain and key on the Docker host, for example:

```bash
sudo install -d -m 755 /etc/appneta-ia-bridge/tls
sudo install -m 644 fullchain.pem /etc/appneta-ia-bridge/tls/cert.pem
sudo install -m 600 privkey.pem   /etc/appneta-ia-bridge/tls/key.pem
```

The certificate must be valid for the DNS name AppNeta will connect to (it must appear in the
certificate's subject alternative names) and must chain to a public CA. With such a certificate the
AppNeta connector can verify the server, so `ignoreSSLErrors` stays `false` (step 6).

Renew the certificate before it expires and restart the container (`docker restart
appneta-ia-bridge`) to load it.

Without a public certificate, see the
[self-signed appendix](#appendix-self-signed-certificate).

## 3. Create the authentication token

The token is a shared secret that you choose. The bridge checks it on every request and AppNeta
sends it. Generate it once into a root-only file:

```bash
sudo install -d -m 700 /etc/appneta-ia-bridge/secrets
openssl rand -hex 32 | sudo tee /etc/appneta-ia-bridge/secrets/auth_token >/dev/null
sudo chmod 600 /etc/appneta-ia-bridge/secrets/auth_token
sudo cat /etc/appneta-ia-bridge/secrets/auth_token     # you will paste this into AppNeta in step 6
```

The container reads the file at start, so the token never appears in `docker inspect` or your shell
history.

## 4. Run the container

For a `general` destination (Spectrum without VNA, Smarts, or another receiver):

```bash
docker run -d \
  --name appneta-ia-bridge \
  --restart unless-stopped \
  -e TRAP_DESTINATION_1_HOST=spectrum.example.com \
  -e TRAP_DESTINATION_1_SCHEME=general \
  -e AUTH_TYPE=BEARER \
  -e AUTH_TOKEN_FILE=/run/secrets/auth_token \
  -v /etc/appneta-ia-bridge/secrets:/run/secrets:ro \
  -v /etc/appneta-ia-bridge/tls:/etc/appneta-ia-bridge/tls:ro \
  -p 443:443 \
  appneta-ia-bridge
```

For a `vna` destination, set the scheme to `vna` and add your AppNeta platform host so the bridge
can compute the 32-bit IDs:

```bash
  -e TRAP_DESTINATION_1_SCHEME=vna \
  -e APPNETA_NIS_SERVER=app-12 \
```

`APPNETA_NIS_SERVER` is the label from your AppNeta URL (`https://app-12.pm.appneta.com` →
`app-12`). If your host is not in the table in `alarm_bridge/config.py`, set
`APPNETA_V3_ID_OFFSET` to the offset between your v4 and v3 IDs instead (see
[`README.md`](README.md#configuration)). The container refuses to start without one of them.

More than one destination: add `TRAP_DESTINATION_2_HOST`, `TRAP_DESTINATION_2_SCHEME`, and so on.
Non-default port or community: `TRAP_DESTINATION_<N>_PORT` and `TRAP_DESTINATION_<N>_COMMUNITY`.

Check that it started:

```bash
docker logs appneta-ia-bridge
```

You should see `appneta-ia-bridge listening on port 8080`. A configuration error is printed there
and the container exits.

## 5. Install the destination side

Copy the bundle from the Docker host to the destination server, then install it there.

### Spectrum

Full prerequisites, backups, and rollback are in
[`spectrum-install/README.md`](spectrum-install/README.md). Read its prerequisites first.

1. Copy the bundle:
   ```bash
   scp -r intelligent_alarms.sh lib spectrum-install spectrum-install-general mibs \
       spectrum-host:/tmp/appneta-ia-bridge-install/
   ```
2. On the Spectrum server, become the account that owns the Spectrum installation (not root) and
   install:
   ```bash
   sudo -u spectrum -i
   cd /tmp/appneta-ia-bridge-install
   ./intelligent_alarms.sh --spectrum --general install      # or --vna
   ./intelligent_alarms.sh --spectrum status                 # "Spectrum: general installed"
   ```
   The installer prints `Using SPECTRUM_HOME=...`; confirm that it is your installation. If your
   `spectrum80.env` is not in `/opt/SPECTRUM`, set `SPECTRUM_ENV_FILE` first.
3. Compile the MIBs (both files must be in the same directory):
   ```bash
   export SPECTRUM_HOME=<the directory printed above>
   mkdir -p /tmp/mib-compile
   cp /tmp/appneta-ia-bridge-install/mibs/*.mib /tmp/mib-compile/
   bash "$SPECTRUM_HOME/MibDatabase/scripts/BulkMibCompile.sh" -d /tmp/mib-compile -f "*.mib"
   ```
4. Restart SpectroSERVER:
   ```bash
   (cd "$SPECTRUM_HOME/bin" && ./stopSS.pl)
   (cd "$SPECTRUM_HOME/bin" && ./startSS.pl)
   ```
5. In OneClick, create an `AppNetaAdmin` model for the bridge: **Locator** → **Universe** →
   **Topology** tab → **Create New Model by Type** → `AppNetaAdmin`. Set **Name** (shown on each
   alarm, for example `AppNeta Intelligent Alarms`) and **Monitoring Point IP Address** (the
   address the bridge sends traps *from*). Then, on the new model's **Attributes** tab, set
   `map_traps_to_this_model_using_IP_header` (`0x3dc000b`) to `Yes`. Without this, traps are not
   matched to the model. Details and a REST equivalent are in
   [`spectrum-install/README.md`](spectrum-install/README.md#step-by-step-install).

Make sure `TRAP_DESTINATION_<N>_SCHEME` in step 4 matches the mode you installed.

### Smarts

Prerequisites and verification are in [`smarts-install/README.md`](smarts-install/README.md).

```bash
scp -r intelligent_alarms.sh lib smarts-install smarts-host:/tmp/appneta-ia-bridge-install/
# on the Smarts server:
cd /tmp/appneta-ia-bridge-install
dmctl -s INCHARGE-OI ping                       # must succeed
./intelligent_alarms.sh --smarts --general install
./intelligent_alarms.sh --smarts status         # "Smarts:   general installed"
```

### Other SNMP trap receivers

Nothing to install. The bridge sends standard SNMP v2c traps; the MIBs in [`mibs/`](mibs/) describe
them. Point `TRAP_DESTINATION_<N>_HOST`, `_PORT` and `_COMMUNITY` at your receiver.

## 6. Create the AppNeta connector

In AppNeta, create an alarm connector of type **Webhook** (see [Alarms —
Connectors](https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/alarms-set-up/alarms-connectors.html)
in the AppNeta documentation):

| Field | Value |
|---|---|
| URL | `https://ia-bridge.example.com/` |
| Method | `POST` |
| Header | `Content-Type: application/json` |
| Authentication | Bearer token: the value from step 3 |
| Notify on | `ALARM_RAISED`, `ALARM_CLEARED` |
| Ignore SSL errors | **off** (`ignoreSSLErrors` = `false`) |

The same connector as JSON, for the API (`POST /api/v4/connector/alarm?orgId=<orgId>` with your
AppNeta API key; `orgId` goes in the query string, not the body):

```json
{
  "name": "appneta-ia-bridge",
  "enabled": true,
  "description": "All alarms forwarded to appneta-ia-bridge.",
  "notifyOn": ["ALARM_RAISED", "ALARM_CLEARED"],
  "connectors": [
    {
      "type": "WEBHOOK",
      "url": "https://ia-bridge.example.com/",
      "method": "POST",
      "headers": [{ "key": "Content-Type", "value": "application/json" }],
      "auth": { "type": "BEARER", "token": "<token from step 3>" },
      "ignoreSSLErrors": false
    }
  ]
}
```

Notes:

- `auth.type` must match `AUTH_TYPE` on the bridge. For `BASIC`, use `username` and `password`; for
  `NONE`, omit `auth`.
- To forward only some alarms, add an `include` and/or `exclude` block (for example by severity,
  policy group, or tag). With neither, every alarm is forwarded.
- A connector downloaded from AppNeta shows secrets as asterisks. Do not upload a downloaded
  connector unchanged; set the header value and token again first.
- Allow inbound `443/tcp` from the internet to the Docker host in every firewall in the path.
  AppNeta delivers from its own cloud, so the source addresses are not yours.

## 7. Verify

1. **The bridge accepts a request.** From any machine that can reach it (add `-k` only if you use a
   self-signed certificate):
   ```bash
   curl -i -X POST https://ia-bridge.example.com/ \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $(sudo cat /etc/appneta-ia-bridge/secrets/auth_token)" \
     -d '{"state":"RAISED","alarmId":"install-verify-1","orgId":"1","ruleId":"1","alarmSeverity":"MAJOR","rule":"Install verification"}'
   ```
   Expect `HTTP/1.1 200`. `401` means the token is wrong; `400` means the payload or the trap
   destinations are invalid (see `docker logs appneta-ia-bridge`). With a verifying client and a
   public certificate this also proves the certificate and name are correct.
2. **The trap arrives.** On the receiver, capture `udp port 162` (or your configured port) while
   you repeat the request.
3. **The alarm appears.** In Spectrum, confirm an alarm whose title starts with `AppNeta
   Intelligent Alarm` and names the rule `Install verification` (`general`); Smarts: `dmctl -s INCHARGE-OI getInstances
   ICS_Notification`.
   A `vna` destination raises the alarm on the matching `NetworkPath`/`WebPath` model, so the test
   payload must carry a real path: set `"itemType"` (`NETWORK_PATH` or `WEB_PATH`) and `"itemId"` to values from your AppNeta
   organization. With a made-up path the trap arrives (step 2) but no alarm appears.
4. **It clears.** Repeat the request with `"state":"CLEARED"` and the same `alarmId`; the alarm
   clears.
5. **Warning severity is an event only.** Repeat with `"alarmSeverity":"WARNING"` and a new
   `alarmId`; an event is logged but no Spectrum alarm is created.
6. **AppNeta delivers.** Trigger or wait for a real alarm; `docker logs -f appneta-ia-bridge` shows
   each accepted request.

## Maintenance

- **Logs:** `docker logs -f appneta-ia-bridge`
- **Change settings:** `docker rm -f appneta-ia-bridge`, then repeat step 4 with the new values.
- **Upgrade:** `git fetch --tags && git checkout <new tag>`, rebuild the image (step 1), recreate
  the container (step 4). If [`CHANGELOG.md`](CHANGELOG.md) lists destination-side changes, run
  `./intelligent_alarms.sh --spectrum uninstall` (or `--smarts`) and install again from the new
  version.
- **Rotate the token:** write a new value to the token file, update the AppNeta connector, and
  `docker restart appneta-ia-bridge`.
- **Uninstall:** `docker rm -f appneta-ia-bridge`; on the destination servers run
  `./intelligent_alarms.sh --spectrum uninstall` or `--smarts uninstall` and restart SpectroSERVER
  (Spectrum). The Spectrum rollback levels are in
  [`spectrum-install/README.md`](spectrum-install/README.md#backout-plan).

## Appendix: self-signed certificate

If you cannot obtain a publicly trusted certificate, a self-signed one works as long as the AppNeta
connector sets `ignoreSSLErrors` to `true`. This disables AppNeta's check of the server's identity,
so use it for evaluation, not production.

```bash
sudo install -d -m 755 /etc/appneta-ia-bridge/tls
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout /etc/appneta-ia-bridge/tls/key.pem -out /etc/appneta-ia-bridge/tls/cert.pem \
  -subj "/CN=ia-bridge.example.com" \
  -addext "subjectAltName=DNS:ia-bridge.example.com"
sudo chmod 600 /etc/appneta-ia-bridge/tls/key.pem
```

Use `IP:<address>` instead of `DNS:<name>` if the connector URL uses an IP address. The name or
address in the certificate must match the connector URL exactly. Then run steps 3 through 7 as
above, with `"ignoreSSLErrors": true` in the connector, and `curl -k` in the verification step.
