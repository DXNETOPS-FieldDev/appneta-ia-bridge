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
| Spectrum **with** the earlier AppNeta (VNA) integration modeling `NetworkPath`/`WebPath` | `vna` | [Spectrum](#spectrum---spectroserver-and-oneclick-on-separate-hosts) (separate hosts) or [single host](#spectrum---spectroserver-and-oneclick-combined-on-a-single-host) |
| Spectrum **without** it (the common case) | `general` | [Spectrum](#spectrum---spectroserver-and-oneclick-on-separate-hosts) (separate hosts) or [single host](#spectrum---spectroserver-and-oneclick-combined-on-a-single-host) |
| Smarts | `general` | [Smarts](#smarts) |
| Any other SNMP trap receiver | `general` | none; see [Other receivers](#other-snmp-trap-receivers) |

Not sure whether your Spectrum has the VNA integration? Look for `NetworkPath` or `WebPath` models
that carry an `AppNetaPathIdentifier` attribute (`0x673009f`), or ask your Spectrum administrator.

## 1. Get the code and build the image

```bash
git clone https://github.com/DXNETOPS-FieldDev/appneta-ia-bridge.git appneta-ia-bridge
cd appneta-ia-bridge
git checkout v0.2.1
docker build -t appneta-ia-bridge .
```

To review the code first, start with the [security summary in `README.md`](README.md#security-summary).

## 2. Install the TLS certificate

**No publicly trusted certificate?** Generate a self-signed one with the
[self-signed appendix](#appendix-self-signed-certificate), then continue at step 3.

If you have a certificate, place the certificate chain and key on the Docker host, for example:

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

## 3. Create the authentication token

The token is a shared secret that you choose. The bridge checks it on every request and AppNeta
sends it. Generate it once into a root-only file:

```bash
sudo install -d -m 700 /etc/appneta-ia-bridge/secrets
openssl rand -hex 32 | sudo tee /etc/appneta-ia-bridge/secrets/auth_token >/dev/null
sudo chmod 600 /etc/appneta-ia-bridge/secrets/auth_token
```

The container reads the file at start, so the token never appears in `docker inspect` or your shell
history.

**You will need this token in step 6** to configure the AppNeta connector; step 6 shows how to display it.

## 4. Run the container

One bridge can forward to several trap receivers at once, for example a Spectrum server and a
Smarts server. Each receiver is a numbered *destination*: every setting that starts with
`TRAP_DESTINATION_1_` configures the first receiver, `TRAP_DESTINATION_2_` the second, and so on.
Run a single `docker run` and repeat the numbered settings once per receiver; do not start a second
container. Each destination has its own `_SOURCE_ADDRESS` because each receiver may identify the
AppNeta device by a different IP (see below). The examples first show one destination, then two.

For a `general` destination (Spectrum without VNA, Smarts, or another receiver):

```bash
docker run -d \
  --name appneta-ia-bridge \
  --restart unless-stopped \
  -e TRAP_DESTINATION_1_HOST=spectrum.example.com \
  -e TRAP_DESTINATION_1_SCHEME=general \
  -e TRAP_DESTINATION_1_SOURCE_ADDRESS=<ip-of-appneta-model> \
  -e AUTH_TYPE=BEARER \
  -e AUTH_TOKEN_FILE=/run/secrets/auth_token \
  -v /etc/appneta-ia-bridge/secrets:/run/secrets:ro \
  -v /etc/appneta-ia-bridge/tls:/etc/appneta-ia-bridge/tls:ro \
  -p 443:443 \
  appneta-ia-bridge
```

For a `vna` destination (Spectrum where the VNA-AppNeta integration already models your paths), the
full command is below. It differs from the `general` command above in two settings:
**`TRAP_DESTINATION_1_SCHEME=vna`**, and the added **`APPNETA_NIS_SERVER`**, your AppNeta platform
host, so the bridge can compute the 32-bit IDs. Everything else is identical.

```bash
docker run -d \
  --name appneta-ia-bridge \
  --restart unless-stopped \
  -e TRAP_DESTINATION_1_HOST=spectrum.example.com \
  -e TRAP_DESTINATION_1_SCHEME=vna \
  -e TRAP_DESTINATION_1_SOURCE_ADDRESS=<ip-of-appneta-model> \
  -e APPNETA_NIS_SERVER=app-12 \
  -e AUTH_TYPE=BEARER \
  -e AUTH_TOKEN_FILE=/run/secrets/auth_token \
  -v /etc/appneta-ia-bridge/secrets:/run/secrets:ro \
  -v /etc/appneta-ia-bridge/tls:/etc/appneta-ia-bridge/tls:ro \
  -p 443:443 \
  appneta-ia-bridge
```

`APPNETA_NIS_SERVER` is the label from your AppNeta URL (`https://app-12.pm.appneta.com` →
`app-12`). If your host is not in the table in `alarm_bridge/config.py`, set
`APPNETA_V3_ID_OFFSET` to the offset between your v4 and v3 IDs instead (see
[`README.md`](README.md#configuration)). The container refuses to start without one of them.

`TRAP_DESTINATION_<N>_SOURCE_ADDRESS` is required for every destination. Set it to the IP address the
receiver uses for the AppNeta device: the IP of the AppNeta model in Spectrum, or the element's IP in
Smarts (for Spectrum, the same IP as the model's Monitoring Point IP Address; for Smarts, the host
element the alarms should attach to). It is sent in each trap so the receiver can identify the device even when the bridge's real
address is hidden or changes: NAT rewrites it, a container or host restart gets a new address, or
the bridge is moved to another host. Without it, the receiver would have to match on the packet's
source IP and the alarms would stop landing on the right object whenever that address drifted. Set it to `none` to send no claim. Do not use the IP address of
another real device: its alarms would be routed to that device.

Non-default port or community: `TRAP_DESTINATION_<N>_PORT` and `TRAP_DESTINATION_<N>_COMMUNITY`.

### Example: Spectrum with VNA and Smarts from one bridge

Destination 1 is Spectrum with VNA-modeled topology, destination 2 is Smarts. The source address for
destination 1 is the IP of the AppNeta model in Spectrum; for destination 2 it is the IP of the host
element in Smarts. `APPNETA_NIS_SERVER` is needed because destination 1 uses the `vna` scheme.

```bash
docker run -d \
  --name appneta-ia-bridge \
  --restart unless-stopped \
  -e TRAP_DESTINATION_1_HOST=spectrum.example.com \
  -e TRAP_DESTINATION_1_SCHEME=vna \
  -e TRAP_DESTINATION_1_SOURCE_ADDRESS=<ip-of-appneta-model-in-spectrum> \
  -e TRAP_DESTINATION_2_HOST=smarts.example.com \
  -e TRAP_DESTINATION_2_SCHEME=general \
  -e TRAP_DESTINATION_2_SOURCE_ADDRESS=<ip-of-host-element-in-smarts> \
  -e APPNETA_NIS_SERVER=app-12 \
  -e AUTH_TYPE=BEARER \
  -e AUTH_TOKEN_FILE=/run/secrets/auth_token \
  -v /etc/appneta-ia-bridge/secrets:/run/secrets:ro \
  -v /etc/appneta-ia-bridge/tls:/etc/appneta-ia-bridge/tls:ro \
  -p 443:443 \
  appneta-ia-bridge
```

Check that it started:

```bash
docker logs appneta-ia-bridge
```

You should see `appneta-ia-bridge listening on port 8080`. A configuration error is printed there
and the container exits.

## 5. Install the destination side

Copy the bundle from the Docker host to the destination server, then install it there.

Which Spectrum layout do you have?

| Layout | Run |
|---|---|
| SpectroSERVER on one host, OneClick on another | `--spectroserver` on the SpectroSERVER host **and** `--oneclick` on the OneClick host, with the same mode (`general` or `vna`) on each. |
| One host runs both SpectroSERVER and OneClick | `--spectroserver` only (see the single-host section below). It installs everything, including the alarm text files OneClick reads. |

Not sure which you have? A host with a `tomcat/` directory under its Spectrum directory runs OneClick.
After a successful `--spectroserver install` on a host without one, the installer prints a reminder to run
`--oneclick` on the OneClick host. It cannot see that host, so it only reminds you; it does not check.

### Spectrum - SpectroSERVER and OneClick on separate hosts

For a split install, copy the bundle to both hosts (step 1 of the single-host procedure below), then:

- **SpectroSERVER host:** steps 2, 4 and 5 of the single-host procedure below (the `AppNetaAdmin` model lives on the
  SpectroSERVER).
- **OneClick host:** become the account that owns the Spectrum installation and run
  ```bash
  cd /tmp/appneta-ia-bridge-install
  ./intelligent_alarms.sh --oneclick --general install     # or --vna, matching the other host
  ./intelligent_alarms.sh --oneclick status                # "Spectrum OneClick host: general installed"
  ```
  This installs only the alarm text files (`SG-Support/CsPCause/Prob*_en_US` and
  `SG-Support/CsEvFormat/Event*_en_US`). OneClick reads them from its own host, live, so nothing
  restarts. Without them the console shows `file Prob... was not found on this server` and events
  read `...for which no event format file exists`. `--oneclick install` also runs the MIB compile, because
  the MIB compiler belongs to the OneClick server.

  `--oneclick install` is refused on a host that has a SpectroSERVER (an `SS/` directory): that host gets
  these files from `--spectroserver`, and `--oneclick` there would duplicate them. Use `--spectroserver` on it.

Run `status` on each host when you finish. If an install stopped before the end (for example a MIB
compile failure), `status` says `INCOMPLETE install`: fix the error and run `install` again.

To switch modes or remove the integration, run the same command on each host (`--oneclick uninstall`
on the OneClick host).

If OneClick reports `No SpectroSERVER is available`, add the OneClick host's name to
`$SPECROOT/.hostrc` on the SpectroSERVER host, and make sure the OneClick host's own `.hostrc` names the
SpectroSERVER host; then restart Tomcat on the OneClick host. That is Spectrum's own setup for a separate
OneClick host, not something this bundle changes.

### Spectrum - SpectroSERVER and OneClick combined on a single host

Use this when one host runs both SpectroSERVER and OneClick. Full prerequisites, backups, and rollback are in
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
   ./intelligent_alarms.sh --spectroserver --general install      # or --vna
   ./intelligent_alarms.sh --spectroserver status                 # "Spectrum: general installed"
   ```
   The installer prints `Using SPECTRUM_HOME=...`; confirm that it is your installation. If your
   `spectrum80.env` is not in `/opt/SPECTRUM`, set `SPECTRUM_ENV_FILE` first.
   Every `install` and `uninstall` also writes a log, `logs/<yyyymmddhhmm>-install.log` (or `-uninstall.log`) next to
   `intelligent_alarms.sh` (the directory is created if missing; set `IA_LOG_DIR` to put it elsewhere).
   It holds the command, user, host, everything the installers printed, and the exit status. `status`
   writes no log.
   The installer also compiles both MIBs (`mibs/*.mib`) into Spectrum's `MibDatabase` and exits `1` if
   either fails to compile; read the install log to confirm. A host with no `MibDatabase` (for example
   the SpectroSERVER host of a split install) skips the compile. There, `--oneclick install` does it
   on the OneClick host. To compile by hand instead, on the host that has `MibDatabase`, with both
   files in the same directory:
   ```bash
   export SPECTRUM_HOME=<the directory printed above>
   mkdir -p /tmp/mib-compile
   cp /tmp/appneta-ia-bridge-install/mibs/*.mib /tmp/mib-compile/
   bash "$SPECTRUM_HOME/MibDatabase/scripts/BulkMibCompile.sh" -d /tmp/mib-compile -f "*.mib"
   ```
3. Restart SpectroSERVER:
   ```bash
   (cd "$SPECTRUM_HOME/bin" && ./stopSS.pl)
   (cd "$SPECTRUM_HOME/bin" && ./startSS.pl)
   ```
4. In OneClick, create an `AppNetaAdmin` model for the bridge: **Locator** → **Universe** →
   **Topology** tab → **Create New Model by Type** → `AppNetaAdmin`. Set **Name** (shown on each
   alarm, for example `AppNeta Intelligent Alarms`) and **Monitoring Point IP Address** (the
   address the bridge's traps are attributed to). With the v0.1.3 sender-identity claim, the
   bridge no longer needs to be the real sender of this address: set
   `TRAP_DESTINATION_<N>_SOURCE_ADDRESS` to this same IP. Do not use the IP of another real device
   already modeled in Spectrum. Then, on the new model's **Attributes** tab, set
   `map_traps_to_this_model_using_IP_header` (`0x3dc000b`) to `Yes`. Without this, traps are not
   matched to the model. Details and a REST equivalent are in
   [`spectrum-install/README.md`](spectrum-install/README.md#step-by-step-install).

Make sure `TRAP_DESTINATION_<N>_SCHEME` in step 4 matches the mode you installed.

### Smarts

Prerequisites and verification are in [`smarts-install/README.md`](smarts-install/README.md).
Smarts matches the traps by OID from its own `trap_mgr.conf`, so it does not need the MIB files.

```bash
scp -r intelligent_alarms.sh lib smarts-install smarts-host:/tmp/appneta-ia-bridge-install/
# on the Smarts server:
cd /tmp/appneta-ia-bridge-install
dmctl -s INCHARGE-OI ping                       # must succeed
./intelligent_alarms.sh --smarts --general install
./intelligent_alarms.sh --smarts status         # "Smarts:   general installed"
```

### Other SNMP trap receivers

Nothing to install on the receiver. The bridge sends standard SNMP v2c traps; the MIBs in
[`mibs/`](mibs/) describe them. Point `TRAP_DESTINATION_<N>_HOST`, `_PORT` and `_COMMUNITY` at your
receiver.

To have your receiver decode the traps by name, copy the two MIB files to it. On the Docker host, in
the directory where you cloned the repository (step 1):

```bash
scp mibs/APPNETA-MIB.mib mibs/APPNETA-INTELLIGENT-ALARM-MIB.mib \
    receiver-host:/path/to/mib/files/
```

Replace `receiver-host` with your receiver's name and `/path/to/mib/files/` with the directory your
receiver loads MIBs from, then load them as your receiver's documentation describes. Load
`APPNETA-MIB` first: `APPNETA-INTELLIGENT-ALARM-MIB` imports it.

## 6. Create the AppNeta connector

In AppNeta, create an alarm connector of type **Webhook** (see [Alarms —
Connectors](https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/alarms-set-up/alarms-connectors.html)
in the AppNeta documentation):

Both forms below need the authentication token from step 3. On the Docker host (the machine where you
created the token file), display it:

```bash
sudo cat /etc/appneta-ia-bridge/secrets/auth_token
```

| Field | Value |
|---|---|
| URL | `https://ia-bridge.example.com/` |
| Method | `POST` |
| Header | `Content-Type: application/json` |
| Authentication | Bearer token: the value from step 3 |
| Notify on | `ALARM_RAISED`, `ALARM_CLEARED` |
| Ignore SSL errors | Depends on your certificate from step 2: **off** (`false`) for a certificate from a CA, **on** (`true`) for the self-signed one |

Set **Ignore SSL errors** (`ignoreSSLErrors`) by where the certificate came from:

- **A certificate issued by a CA** (a corporate or public CA that AppNeta's servers trust): `false`, so
  AppNeta verifies the bridge's identity.
- **The self-signed certificate you generated in step 2**: `true`, because AppNeta cannot verify it.
  This is for evaluation, not production.

The same connector as JSON, for the API, with `ignoreSSLErrors` set for a CA-issued certificate (`POST /api/v4/connector/alarm?orgId=<orgId>` with your
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

To forward only some alarms, add an `include` and/or `exclude` block (for example by severity, policy
group, monitoring point type, or tag). With neither, every alarm is forwarded. The following example
adds an `exclude` block to leave out alarms from Windows and Mac software-agent monitoring points;
everything else is unchanged:

```json
{
  "name": "appneta-ia-bridge",
  "enabled": true,
  "description": "All alarms except Windows and macOS agents, forwarded to appneta-ia-bridge.",
  "exclude": { "monitoringPointType": ["Windows", "macOS"] },
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

The type strings must match exactly, including case (`Windows`, `macOS`); a misspelled value matches
nothing and silently excludes nothing.

Notes:

- `auth.type` must match `AUTH_TYPE` on the bridge. For `BASIC`, use `username` and `password`; for
  `NONE`, omit `auth`.
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
   Expect `HTTP/1.1 200`. `401` means the token is wrong; `400` means the payload was rejected
   (see `docker logs appneta-ia-bridge`); `502` means the bridge could not send the trap to any
   destination (check the destination host, port and network). With a verifying client and a
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
6. **AppNeta delivers.** Trigger or wait for a real alarm, and watch the bridge's log:
   ```bash
   docker logs -f appneta-ia-bridge
   ```
   Each webhook the AppNeta connector delivers adds a line like this (with a date and time in front):
   ```
   INFO 127.0.0.1 - "POST / HTTP/1.0" 200 -
   ```
   That line means AppNeta's webhook reached the bridge and it accepted the request. `127.0.0.1` is
   the web server inside the container handing the request to the bridge, not AppNeta's address.
   A `401` in place of `200` means the connector's token is wrong. No line at all means nothing is
   reaching the bridge: check the connector's URL, the certificate (step 2 and `ignoreSSLErrors` in
   step 6), and the firewall.

   A `200` only means the bridge accepted the webhook. If it could not deliver the trap to a
   destination, it logs a warning after the request line, with the reason at the bottom:
   ```
   INFO 127.0.0.1 - "POST / HTTP/1.0" 200 -
   WARNING Failed to send trap to spectrum.example.com:162
   Traceback (most recent call last):
     ...
   socket.gaierror: [Errno -2] Name or service not known
   ```
   `Name or service not known` means the container cannot resolve that destination's hostname: use
   the server's IP address in `TRAP_DESTINATION_<N>_HOST`, or fix DNS on the Docker host, then
   recreate the container (step 4). With several destinations the request returns `200` as long as
   one of them was sent, so check the log for a warning naming each destination. The bridge returns
   `502` only when every destination failed. Traps are UDP: a warning-free log proves the bridge
   sent the trap, not that the receiver got it. Confirm that on the receiver (items 2 and 3).

## Maintenance

- **Logs:** `docker logs -f appneta-ia-bridge`
- **Change settings:** `docker rm -f appneta-ia-bridge`, then repeat step 4 with the new values.
- **Upgrade:** `git fetch --tags && git checkout <new tag>`, rebuild the image (step 1), recreate
  the container (step 4). If [`CHANGELOG.md`](CHANGELOG.md) lists destination-side changes, run
  `./intelligent_alarms.sh --spectroserver uninstall` (or `--smarts`) and install again from the new
  version.
- **Rotate the token:** write a new value to the token file, update the AppNeta connector, and
  `docker restart appneta-ia-bridge`.
- **Uninstall:** `docker rm -f appneta-ia-bridge`; on the destination servers run
  `./intelligent_alarms.sh --spectroserver uninstall` or `--smarts uninstall` and restart SpectroSERVER
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
