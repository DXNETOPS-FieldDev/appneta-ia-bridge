# Installation

This guide installs the bridge container, connects an AppNeta alarm connector to it, and installs
the matching configuration on your Spectrum or Smarts server. Allow about an hour. It uses the
default settings; to tune them afterwards see [`REFERENCE.md`](REFERENCE.md) and the configuration
table in [`README.md`](README.md#configuration).

## Before you start

You need:

- A Linux host with Docker installed (and `git`, if you clone in step 1), reachable from the internet on `443/tcp`.
- A DNS name for that host (examples below use `ia-bridge.example.com`).
- A TLS certificate and private key for that name. Either works: one from a certificate authority
  (public or your organization's own), or a self-signed one you generate in the
  [appendix](#appendix-self-signed-certificate). A CA-issued file must contain the full chain
  (your certificate followed by the intermediates).
- Network access from that host to your trap receiver on UDP `162` (or your configured port). For
  Spectrum the receiver is the SpectroSERVER host.
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

**Run on: the Docker host.**

Pick one way to get the code onto the Docker host. Both end in the same `appneta-ia-bridge`
directory.

**Option A: clone on the Docker host.** Use this if the host can reach the repository directly.

```bash
git clone https://github.com/DXNETOPS-FieldDev/appneta-ia-bridge.git appneta-ia-bridge
cd appneta-ia-bridge
git checkout v0.4.0
```

**Option B: download on your workstation and copy it over.** Use this if the Docker host cannot
reach the repository, or only your workstation can (for example, access needs a VPN that the host
is not on). `git` is not needed on the host.

1. On your workstation, open the repository in a browser, switch to the tag `v0.4.0`, and
   download the source as a `.zip` archive (Code, then Download ZIP). Note the file name.
2. Copy the archive to the Docker host (replace the file name, user and host):

   ```bash
   scp appneta-ia-bridge-0.4.0.zip admin@ia-bridge.example.com:
   ```

3. On the Docker host, unpack it into `appneta-ia-bridge`. The archive contains one top-level
   directory whose name varies, so unpack to a scratch directory and move that directory into
   place (install `unzip` first if the host lacks it):

   ```bash
   unzip -q appneta-ia-bridge-0.4.0.zip -d unpacked
   mv unpacked/* appneta-ia-bridge
   rmdir unpacked
   cd appneta-ia-bridge
   ```

Then build the image:

```bash
docker build -t appneta-ia-bridge .
```

To review the code first, start with the [security summary in `README.md`](README.md#security-summary).

## 2. Install the TLS certificate

**Run on: the Docker host.** The certificate and key live there; AppNeta connects to this host.

Pick one:

- **A certificate from a CA** (public, or your organization's private CA): use it as below.
- **A self-signed certificate:** generate one with the [self-signed appendix](#appendix-self-signed-certificate),
  then continue at step 3. It works the same; AppNeta is told to accept it in step 6.

Place the certificate chain and key on the Docker host, for example:

```bash
sudo install -d -m 755 /etc/appneta-ia-bridge/tls
sudo install -m 644 fullchain.pem /etc/appneta-ia-bridge/tls/cert.pem
sudo install -m 640 -g 10001 privkey.pem /etc/appneta-ia-bridge/tls/key.pem
```

The certificate's subject alternative names must include the exact host in the connector URL (step 6):
the DNS name, or the IP address if the URL uses one. This holds for self-signed certificates too;
`ignoreSSLErrors` does not relax it. Check it now, using the line that matches your URL:

```bash
sudo openssl x509 -in /etc/appneta-ia-bridge/tls/cert.pem -noout -checkhost <dns-name-in-connector-url>
sudo openssl x509 -in /etc/appneta-ia-bridge/tls/cert.pem -noout -checkip <ip-address-in-connector-url>
```

It must print `does match certificate`. On `does NOT match`, AppNeta stops the TLS handshake at the
certificate, nothing reaches the bridge and its logs stay empty: fix the certificate before step 3.
A CA-issued certificate that AppNeta's servers trust keeps `ignoreSSLErrors` `false` in step 6; a
self-signed one (or one from a CA AppNeta does not trust) needs `true`.

The container runs as the unprivileged uid/gid `10001`, never root. Every file you mount into it
(the key above, the token and community in step 3) must therefore be readable by group `10001`:
owner `root`, group `10001`, mode `640`. Nothing is world-readable. The container listens on
`8443` internally; the `-p 443:8443` in step 4 publishes it as `443`.

The certificate **directory** must also be enterable by that user (mode `755`, as `install -d -m 755`
above gives you). A directory created under a restrictive umask (`0700`, owned by root) makes the files
inside it unreadable even when their own modes are fine. Check before you start the container:

```bash
docker run --rm --entrypoint sh -v /etc/appneta-ia-bridge/tls:/etc/appneta-ia-bridge/tls:ro appneta-ia-bridge \
  -c 'cat /etc/appneta-ia-bridge/tls/cert.pem /etc/appneta-ia-bridge/tls/key.pem >/dev/null && echo TLS_READ_OK'
```

If the container refuses to start, its error says whether the directory is missing, cannot be entered, or
a file is missing or unreadable, and prints the modes and owners it sees.

Renew the certificate before it expires and restart the container (`docker restart
appneta-ia-bridge`) to load it.

## 3. Create the authentication token

**Run on: the Docker host.**

The token is a shared secret that you choose. The bridge checks it on every request and AppNeta
sends it. Generate it once into a file only root and the container's group can read:

```bash
sudo install -d -m 750 -g 10001 /etc/appneta-ia-bridge/secrets
openssl rand -hex 32 | sudo tee /etc/appneta-ia-bridge/secrets/auth_token >/dev/null
sudo chgrp 10001 /etc/appneta-ia-bridge/secrets/auth_token
sudo chmod 640 /etc/appneta-ia-bridge/secrets/auth_token
```

The container reads the file at start, so the token never appears in `docker inspect` or your shell
history.

For monitoring, create a second, separate read-only token the same way. It opens `/metrics` and nothing
else, and the webhook token is never accepted there. Skip this and delete the `METRICS_AUTH_TOKEN_FILE`
line in step 4 if you do not want `/metrics`.

```bash
openssl rand -hex 32 | sudo tee /etc/appneta-ia-bridge/secrets/metrics_token >/dev/null
sudo chgrp 10001 /etc/appneta-ia-bridge/secrets/metrics_token
sudo chmod 640 /etc/appneta-ia-bridge/secrets/metrics_token
```

The trap receiver's SNMP community string goes in a third file the same way. Type it when prompted (bash), so
it never appears on a command line or in your shell history:

```bash
read -rsp 'SNMP community: ' c; echo
printf '%s\n' "$c" | sudo tee /etc/appneta-ia-bridge/secrets/trap_community >/dev/null; unset c
sudo chgrp 10001 /etc/appneta-ia-bridge/secrets/trap_community
sudo chmod 640 /etc/appneta-ia-bridge/secrets/trap_community
```

**You will need the webhook token in step 6** to configure the AppNeta connector; step 6 shows how to display it.

## 4. Run the container

**Run on: the Docker host.** `<receiver-host>` below is the Spectrum or Smarts server, not the Docker host.

One bridge can forward to several trap receivers at once, for example a Spectrum server and a
Smarts server. Each receiver is a numbered *destination*: every setting that starts with
`TRAP_DESTINATION_1_` configures the first receiver, `TRAP_DESTINATION_2_` the second, and so on.
Use a single `docker run` and repeat the numbered settings once per receiver; do not start a second
container.

```bash
# Fill in both <...> values before running:
#   <receiver-host>       the host that receives the traps (SpectroSERVER, or the Smarts trap adapter host)
#   <ip-of-appneta-model> must equal the Monitoring Point IP Address of the AppNetaAdmin model (step 5)
docker run -d \
  --name appneta-ia-bridge \
  --restart unless-stopped \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v ia-bridge-state:/var/lib/appneta-ia-bridge \
  -e TRAP_DESTINATION_1_HOST=<receiver-host> \
  -e TRAP_DESTINATION_1_COMMUNITY_FILE=/run/secrets/trap_community \
  -e TRAP_DESTINATION_1_SCHEME=general \
  -e TRAP_DESTINATION_1_SOURCE_ADDRESS=<ip-of-appneta-model> \
  -e AUTH_TYPE=BEARER \
  -e AUTH_TOKEN_FILE=/run/secrets/auth_token \
  -e METRICS_AUTH_TOKEN_FILE=/run/secrets/metrics_token \
  -v /etc/appneta-ia-bridge/secrets:/run/secrets:ro \
  -v /etc/appneta-ia-bridge/tls:/etc/appneta-ia-bridge/tls:ro \
  -p 443:8443 \
  appneta-ia-bridge
```

What the lines do. The defaults are right for most sites; [`REFERENCE.md`](REFERENCE.md) covers changing them.

| Line | Purpose |
|---|---|
| `TRAP_DESTINATION_1_*` | The receiver (Spectrum, Smarts or another) and how to talk to it; explained below. |
| `-v ia-bridge-state:/var/lib/appneta-ia-bridge` | **Duplicate suppression and alarm spool.** AppNeta can deliver the same alarm more than once; the bridge sends each distinct alarm once. If a receiver is down it stores alarms here and delivers them when the receiver returns. The named volume keeps this across restarts and upgrades. Do not remove this line. |
| `--log-opt max-size=10m --log-opt max-file=3` | **Logging.** The bridge logs to `docker logs`. Docker never rotates that log by default, so this caps it at 30 MB. |
| `METRICS_AUTH_TOKEN_FILE` | **Monitoring.** Turns on `/metrics` for Prometheus and Grafana, protected by the read-only token from step 3. Leave this line out to keep `/metrics` off. |

Optional, add to the command if you want it: `-e LOG_SYSLOG_ADDRESS=<collector-host>:514` also sends the
bridge's log lines to a syslog collector. See [Logging](REFERENCE.md#logging).

**A `vna` destination** (Spectrum where the earlier VNA-AppNeta integration already models your paths)
differs in two settings: `TRAP_DESTINATION_1_SCHEME=vna`, and an added
`-e APPNETA_NIS_SERVER=app-12`, your AppNeta platform host, so the bridge can compute the 32-bit IDs.
`APPNETA_NIS_SERVER` is the label from your AppNeta URL (`https://app-12.pm.appneta.com` → `app-12`). If
your host is not in the table in `alarm_bridge/config.py`, set `APPNETA_V3_ID_OFFSET` to the offset
between your v4 and v3 IDs instead (see [`README.md`](README.md#configuration)). The container refuses to
start without one of them.

**A second destination** (for example Smarts alongside Spectrum) adds one more set of numbered
settings to the same command, each with its own `_SOURCE_ADDRESS`:

```bash
  -e TRAP_DESTINATION_2_HOST=smarts.example.com \
  -e TRAP_DESTINATION_2_COMMUNITY_FILE=/run/secrets/trap_community \
  -e TRAP_DESTINATION_2_SCHEME=general \
  -e TRAP_DESTINATION_2_SOURCE_ADDRESS=<ip-of-host-element-in-smarts> \
```

`TRAP_DESTINATION_<N>_HOST` is the hostname or IP address of the server that **receives the SNMP traps**
(UDP port 162 by default; override with `TRAP_DESTINATION_<N>_PORT`):

- **Spectrum:** the **SpectroSERVER** host, the one that runs the SpectroSERVER process and the `AppNetaAdmin`
  model. Not the OneClick web server, unless OneClick runs on the same host. If SpectroSERVER and OneClick
  are on separate hosts, use the SpectroSERVER host; OneClick never receives the traps. In a distributed
  SpectroSERVER environment that uses Trap Director, use the SpectroSERVER designated as the Trap Director
  server: it forwards each trap to the SpectroSERVER that models the device.
- **Smarts:** the host that runs the Smarts SNMP trap adapter (`sm_trapd`).

The bridge host needs a network path to that address on UDP 162 (firewalls included).

`TRAP_DESTINATION_<N>_SOURCE_ADDRESS` is required for every destination: the IP address the receiver
already knows the AppNeta device by. Do not use the IP of another real device (its alarms would be routed
to it); `none` sends no claim.

- **Spectrum:** the **Monitoring Point IP Address** of the `AppNetaAdmin` model you create in step 5.
  The simplest choice is the IP address of this Docker host. Enter the same address as the model's
  Monitoring Point IP Address in step 5. If they differ, the alarms do not appear on the model.
- **Smarts:** the IP of the Host element the alarms should attach to.

Why it exists, a worked example and how Smarts uses it: [`REFERENCE.md`](REFERENCE.md#choosing-the-trap-source-address).

`TRAP_DESTINATION_<N>_COMMUNITY_FILE` (or `TRAP_DESTINATION_<N>_COMMUNITY`) is **required**: the SNMP
community string the receiver expects, with no default. Put it in a file next to the other secrets
(for example `/etc/appneta-ia-bridge/secrets/trap_community`, group `10001`, mode 640) so it does not show up in
`docker inspect`. A non-default port is `TRAP_DESTINATION_<N>_PORT`.

Check that it started:

```bash
docker logs appneta-ia-bridge
```

You should see `appneta-ia-bridge listening on port 8080`. A configuration error is printed there
and the container exits.

### Duplicate suppression and the alarm spool

AppNeta can deliver the same alarm event more than once. The bridge sends each distinct event (alarm id,
state and timestamp) as a trap once and answers repeats with `200`, so the receiver never sees a storm. If a
trap receiver is down, the bridge stores each alarm on disk, answers AppNeta `200` so AppNeta stops retrying,
and delivers the stored alarms, oldest first, when the receiver comes back. Both live in the state volume,
which is on by default: the `-v ia-bridge-state:/var/lib/appneta-ia-bridge` line in the command above.

Where that volume comes from decides whether alarms survive an upgrade. The named volume above is the
recommended choice. To keep the state in a host directory instead, create it and swap the line:

```bash
sudo install -d -o 10001 /srv/ia-bridge-state
#   -v /srv/ia-bridge-state:/var/lib/appneta-ia-bridge \
```

Leaving the `-v` line out gives an anonymous volume that an upgrade (`docker rm`, then `docker run`) throws
away, so spooled alarms are lost. The defaults remember an event for 96 hours and cap the spool at 256 MB.
To change either, add a setting to the command, for example a week of memory and a 512 MB spool:

```bash
  -e DEDUP_TTL_HOURS=168 \
  -e SPOOL_MAX_MB=512 \
```

To name each destination so stored alarms stay with the right receiver if you later reorder them, add
`-e TRAP_DESTINATION_1_NAME=spectrum` (and `_2_NAME` for a second one). Every setting, the disk limits, how
the bridge decides a destination is down, and the choice of volume are in
[`REFERENCE.md`](REFERENCE.md#state-volume-and-the-alarm-spool).

### Logging

The bridge writes to `docker logs`: a one-line summary every minute while alarms arrive, a line per
destination, and an event when a destination goes down or comes back. It never logs alarm contents or
secrets. The `--log-opt` pair in the command keeps Docker from filling the disk. Watch it with:

```bash
docker logs -f appneta-ia-bridge
```

A destination that stops accepting traps shows as `event=dest_down` with the reason, then `event=dest_up`
when it recovers. To also send the bridge's lines to a syslog collector, add one line to the command (`tcp`
is optional; the default is `udp`):

```bash
  -e LOG_SYSLOG_ADDRESS=collector.example.com:514 \
  -e LOG_SYSLOG_PROTOCOL=tcp \
```

For more detail while troubleshooting, add `-e LOG_LEVEL=DEBUG` (one extra line per request). The full list
of log events, forwarding to several collectors, and the Docker log-driver alternative are in
[`REFERENCE.md`](REFERENCE.md#logging).

### Monitoring with Prometheus and Grafana

With the `METRICS_AUTH_TOKEN_FILE` line from the command, the bridge serves counters and gauges (alarms
received and delivered, duplicates, send failures, per-destination up or down, spool depth) at
`https://ia-bridge.example.com/metrics`. Only the read-only metrics token opens it; the webhook token never
does. If you skipped it in step 3, create the token now (it must differ from the webhook token), add
`-e METRICS_AUTH_TOKEN_FILE=/run/secrets/metrics_token \` to the command, and recreate the container:

```bash
openssl rand -hex 32 | sudo tee /etc/appneta-ia-bridge/secrets/metrics_token >/dev/null
sudo chgrp 10001 /etc/appneta-ia-bridge/secrets/metrics_token
sudo chmod 640 /etc/appneta-ia-bridge/secrets/metrics_token
```

Check it from the Docker host without putting the token on a command line:

```bash
sudo sh -c 'printf "Authorization: Bearer %s\n" "$(cat /etc/appneta-ia-bridge/secrets/metrics_token)"' \
  | curl --cacert /etc/appneta-ia-bridge/tls/cert.pem -H @- https://ia-bridge.example.com/metrics
```

Point a Prometheus scrape job at it with the token file as a bearer credential, and alert first on
`appneta_ia_bridge_destination_up == 0` (a receiver is down) and
`rate(appneta_ia_bridge_webhooks_received_total[15m]) == 0` (nothing is arriving from AppNeta). The scrape
job, every series and the full alert list are in
[`REFERENCE.md`](REFERENCE.md#metrics-for-prometheus-and-grafana).

## 5. Install the destination side

**Run on: the Spectrum or Smarts server** (the Docker host only for the copy). Each sub-section says which.

First copy the bundle from the Docker host to the destination server, then install it there.

### Copy the bundle to the destination server

On the Docker host, in the directory where you cloned the repository (step 1), copy the bundle to each
server that needs the installer. The `mkdir` makes sure the target directory exists, because `scp`
with several files fails if it does not.

For Spectrum (replace `spectrum-host` with the server's name; repeat for the OneClick host if it is a
separate machine):

```bash
ssh spectrum-host 'mkdir -p /tmp/appneta-ia-bridge-install'
scp -r intelligent_alarms.sh lib spectrum-install spectrum-install-general mibs \
    spectrum-host:/tmp/appneta-ia-bridge-install/
```

For Smarts, use the copy command in the [Smarts](#smarts) section. For other receivers there is nothing
to install; see [Other receivers](#other-snmp-trap-receivers).

### Which Spectrum layout do you have?

| Layout | Run |
|---|---|
| SpectroSERVER on one host, OneClick on another | `--spectroserver` on the SpectroSERVER host **and** `--oneclick` on the OneClick host, with the same mode (`general` or `vna`) on each. |
| One host runs both SpectroSERVER and OneClick | `--spectroserver` only (see the single-host section below). It installs everything, including the alarm text files OneClick reads. |

The bridge sends its traps to a **SpectroSERVER** host, so `TRAP_DESTINATION_<N>_HOST` (step 4) is that
host in both layouts. The OneClick host receives no traps; it only needs the alarm text files above.

If you run several SpectroSERVERs with Trap Director, point the bridge at the Trap Director SpectroSERVER and
install the integration (`--spectroserver`) on the SpectroSERVER that holds the `AppNetaAdmin` model. Trap
Director matches each trap to a model by the agent address in the trap, so after the install, confirm in
step 7 that an alarm appears on the `AppNetaAdmin` model.

Not sure which you have? A host with a `tomcat/` directory under its Spectrum directory runs OneClick.
After a successful `--spectroserver install` on a host without one, the installer prints a reminder to run
`--oneclick` on the OneClick host. It cannot see that host, so it only reminds you; it does not check.

### Spectrum - SpectroSERVER and OneClick on separate hosts

For a split install, copy the bundle to both hosts (see [Copy the bundle](#copy-the-bundle-to-the-destination-server)
above), then run the procedure for each host. The SpectroSERVER steps below repeat the single-host
procedure on purpose, so this section can be followed on its own.

**SpectroSERVER host** (also the host `TRAP_DESTINATION_<N>_HOST` points at):

1. Become the account that owns the Spectrum installation (not root) and install:
   ```bash
   sudo -u spectrum -i
   cd /tmp/appneta-ia-bridge-install
   ./intelligent_alarms.sh --spectroserver --general install      # or --vna
   ./intelligent_alarms.sh --spectroserver status                 # "Spectrum: general installed"
   ```
   The installer prints `Using SPECTRUM_HOME=...`; confirm that it is your installation. If your
   `spectrum80.env` is not in `/opt/SPECTRUM`, set `SPECTRUM_ENV_FILE` first. Every `install` and
   `uninstall` writes a log, `logs/<yyyymmddhhmm>-install.log`, next to `intelligent_alarms.sh` (set
   `IA_LOG_DIR` to put it elsewhere). This host has no `MibDatabase`, so the installer skips the MIB
   compile here; `--oneclick install` does it on the OneClick host.
2. Restart SpectroSERVER. If `SPECTRUM_HOME` is not set in your shell, set it to the directory
   the installer printed:
   ```bash
   (cd "$SPECTRUM_HOME/bin" && ./stopSS.pl)
   (cd "$SPECTRUM_HOME/bin" && ./startSS.pl)
   ```
3. In OneClick, create an `AppNetaAdmin` model for the bridge: **Locator** → **Universe** →
   **Topology** tab → **Create New Model by Type** → `AppNetaAdmin`. Set **Name** (shown on each
   alarm, for example `AppNeta Intelligent Alarms`) and **Monitoring Point IP Address** (the address
   the bridge's traps are attributed to; the simplest choice is the IP address of the Docker host). Set
   `TRAP_DESTINATION_<N>_SOURCE_ADDRESS` (step 4) to this same IP. Do not use the IP of another real device already modeled in Spectrum. Then, on the new
   model's **Attributes** tab, set `map_traps_to_this_model_using_IP_header` (`0x3dc000b`) to `Yes`.
   Without this, traps are not matched to the model.

**OneClick host:**

Become the account that owns the Spectrum installation and run:
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
[`spectrum-install/README.md`](spectrum-install/README.md). Read its prerequisites first, and copy the bundle
to the server as shown in [Copy the bundle](#copy-the-bundle-to-the-destination-server).

1. On the Spectrum server, become the account that owns the Spectrum installation (not root) and
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
   on the OneClick host. You do not compile the MIBs yourself; the manual procedure is in
   [`REFERENCE.md`](REFERENCE.md#compiling-the-mibs-by-hand) for the rare case you need it.
2. Restart SpectroSERVER:
   ```bash
   (cd "$SPECTRUM_HOME/bin" && ./stopSS.pl)
   (cd "$SPECTRUM_HOME/bin" && ./startSS.pl)
   ```
3. In OneClick, create an `AppNetaAdmin` model for the bridge: **Locator** → **Universe** →
   **Topology** tab → **Create New Model by Type** → `AppNetaAdmin`. Set **Name** (shown on each
   alarm, for example `AppNeta Intelligent Alarms`) and **Monitoring Point IP Address** (the
   address the bridge's traps are attributed to; the simplest choice is the IP address of the Docker host).
   With the v0.1.3 sender-identity claim, the
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
ssh smarts-host 'mkdir -p /tmp/appneta-ia-bridge-install'
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

**Do in: the AppNeta web interface**, in a browser. The only values from your servers are the Docker host's address and the token from step 3.

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

- `auth.type` must match `AUTH_TYPE` on the bridge. For `BASIC`, use `username` and `password`. For
  `HEADER`, omit `auth` and add `{"key": "<AUTH_HEADER_NAME>", "value": "<AUTH_TOKEN>"}` to the
  connector's `headers` list. The bridge has no unauthenticated mode.
- A connector downloaded from AppNeta shows secrets as asterisks. Do not upload a downloaded
  connector unchanged; set the header value and token again first.
- Allow inbound `443/tcp` from the internet to the Docker host in every firewall in the path.
  AppNeta delivers from its own cloud, so the source addresses are not yours.

## 7. Verify

**Run on: any machine that can reach the Docker host** for the webhook checks; the Docker host for `docker logs`; the Spectrum or Smarts server to see the alarm.

1. **The bridge accepts a request.** From any machine that can reach it (add `-k` only if you use a
   self-signed certificate):
   ```bash
   curl -i -X POST https://ia-bridge.example.com/ \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $(sudo cat /etc/appneta-ia-bridge/secrets/auth_token)" \
     -d '{"state":"RAISED","alarmId":"install-verify-1","raisedTime":"2026-01-01T00:00:00.000+0000","orgId":"1","ruleId":"1","alarmSeverity":"MAJOR","rule":"Install verification"}'
   ```
   Expect `HTTP/1.1 200`. `401` means the token is wrong (the summary counts it as `auth_failed`); `400` means the payload was rejected
   (see `docker logs appneta-ia-bridge`); `502` means spooling is off and the bridge could not send the trap to any
   destination (check the destination host, port and network); `503` means the spool could not store the alarm
   (check `event=spool_error` and the state volume). With spooling on, a down destination still returns `200`;
   look for `event=dest_down` in the log. With a verifying client and a
   public certificate this also proves the certificate and name are correct.
2. **The trap arrives.** Capture the trap's UDP port (162 unless you set another) while you repeat
   the request, on the Docker host and on the receiver:
   ```bash
   sudo tcpdump -ni any udp port 162
   ```
   Seen on the Docker host but not on the receiver: the network or a firewall is dropping it. Seen on
   both but no alarm: the receiver's configuration (step 5). If the capture on the Docker host is
   followed by `ICMP ... udp port 162 unreachable`, the receiver host is up but nothing is listening
   on that port. On the receiver, `ss -ulnp | grep ':162 '` lists the listener; from the Docker host,
   `nc -zvu -w2 <receiver> 162` reports a closed port (it cannot confirm an open one, because UDP has
   no handshake).
3. **The alarm appears.** In Spectrum, confirm an alarm whose title starts with `AppNeta
   Intelligent Alarm` and names the rule `Install verification` (`general`); Smarts: `dmctl -s INCHARGE-OI getInstances
   ICS_Notification`.
   A `vna` destination raises the alarm on the matching `NetworkPath`/`WebPath` model, so the test
   payload must carry a real path: set `"itemType"` (`NETWORK_PATH` or `WEB_PATH`) and `"itemId"` to values from your AppNeta
   organization. With a made-up path the trap arrives (step 2) but no alarm appears.
4. **A repeat is suppressed.** Send the identical request again. It returns `200` but sends no second trap;
   within about 60 seconds the summary line shows `duplicates=1`. The bridge recognizes a repeat by `alarmId`,
   `state` and the event time (`raisedTime`, or `clearedTime` for a clear). A request without that time still
   works but is counted as `undedupable` and is never suppressed.
5. **It clears.** Repeat the request with `"state":"CLEARED"`, `"clearedTime":"2026-01-01T00:05:00.000+0000"`
   and the same `alarmId`; the alarm clears.
6. **Warning severity is an event only.** Repeat with `"alarmSeverity":"WARNING"` and a new
   `alarmId`; an event is logged but no Spectrum alarm is created.
7. **AppNeta delivers.** Trigger or wait for a real alarm, and watch the bridge's log:
   ```bash
   docker logs -f appneta-ia-bridge
   ```
   At the default log level the bridge does not log one line per request. Within about 60 seconds of
   a webhook arriving you should see a summary line (with a date and time in front):
   ```
   INFO event=summary window=60s received=1 delivered=1 duplicates=0 ...
   ```
   `received` counts authenticated, parseable requests; auth failures and malformed requests are
   counted in `auth_failed` and `rejected`. `delivered` counts those sent as a trap to at
   least one destination. No summary line at all means nothing is reaching the bridge: check the
   connector's URL, the certificate (step 2 and `ignoreSSLErrors` in step 6), the token, and the
   firewall. A wrong token does not stop the summaries: it shows as `auth_failed` above zero and a
   `WARNING event=auth_failing` line each minute, so compare the connector's token with step 3. To see every request as it arrives, recreate the container with `-e LOG_LEVEL=DEBUG`
   (step 4); remove it again afterwards.

   A `200` only means the bridge accepted the webhook. If it could not deliver the trap to a
   destination, the summary shows `send_failed` above zero and a per-destination line shows the
   reason, and a destination that starts failing is logged once:
   ```
   INFO event=dest dest=spectrum.example.com:162 ok=0 failed=1 last=gaierror: [Errno -2] Name or service not known
   WARNING event=dest_state dest=spectrum.example.com:162 state=FAILING reason=gaierror: [Errno -2] Name or service not known
   ```
   `Name or service not known` means the container cannot resolve that destination's hostname: use
   the server's IP address in `TRAP_DESTINATION_<N>_HOST`, or fix DNS on the Docker host, then
   recreate the container (step 4). With several destinations the request returns `200` as long as
   one of them was sent, so check for a `dest` line for each destination. The bridge returns
   `502` only when spooling is off and every destination failed. Traps are UDP: a log with no failures proves the bridge
   sent the trap, not that the receiver got it. Confirm that on the receiver (items 2 and 3).

## Maintenance

- **Logs:** `docker logs -f appneta-ia-bridge`
- **Change settings:** `docker rm -f appneta-ia-bridge`, then repeat step 4 with the new values.
- **Upgrade:** `git fetch --tags && git checkout <new tag>`, rebuild the image (step 1), recreate
  the container (step 4). If [`CHANGELOG.md`](CHANGELOG.md) lists destination-side changes, run
  `./intelligent_alarms.sh --spectroserver uninstall` (or `--smarts`) and install again from the new
  version.
- **Roll back:** keep the previous image with `docker tag appneta-ia-bridge appneta-ia-bridge:previous` before you
  rebuild, then recreate the container from `appneta-ia-bridge:previous` if the new version misbehaves. Full upgrade and
  rollback steps are in [`README.md`](README.md#operate-and-maintain).
- **Rotate the token:** write a new value to the token file, update the AppNeta connector, and
  `docker restart appneta-ia-bridge`.
- **Uninstall:** `docker rm -f appneta-ia-bridge`; on the destination servers run
  `./intelligent_alarms.sh --spectroserver uninstall` or `--smarts uninstall` and restart SpectroSERVER
  (Spectrum). The Spectrum rollback levels are in
  [`spectrum-install/README.md`](spectrum-install/README.md#backout-plan).

## Appendix: self-signed certificate

If you have no CA-issued certificate, a self-signed one works as long as the AppNeta
connector sets `ignoreSSLErrors` to `true`, and the certificate names the host in the connector URL (see
step 2). `ignoreSSLErrors` lets AppNeta accept a certificate it cannot trace to a trusted CA; it does not
excuse a name that does not match. Use it for evaluation, not production.

```bash
sudo install -d -m 755 /etc/appneta-ia-bridge/tls
# The SAN must be the host in the connector URL: DNS:<name>, or IP:<address> if the URL uses an IP.
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout /etc/appneta-ia-bridge/tls/key.pem -out /etc/appneta-ia-bridge/tls/cert.pem \
  -subj "/CN=ia-bridge.example.com" \
  -addext "subjectAltName=DNS:ia-bridge.example.com"
sudo chgrp 10001 /etc/appneta-ia-bridge/tls/key.pem
sudo chmod 640 /etc/appneta-ia-bridge/tls/key.pem
```

Use `IP:<address>` instead of `DNS:<name>` if the connector URL uses an IP address. The name or
address in the certificate must match the connector URL exactly. Then run steps 3 through 7 as
above, with `"ignoreSSLErrors": true` in the connector, and `curl -k` in the verification step.
