# Changelog

## v0.4.0

Alarm spool, per-destination delivery and destination health, a Prometheus `/metrics` endpoint, exact duplicate locking, a rebuilt base image, and reorganized documentation. Read the spool entry's behavior change (the state volume) before upgrading.

Deduplication: the per-alarm lock is now exact, so a slow send to one destination no longer delays unrelated alarms
that happened to share a lock; the summary line names the first rejected-payload reason of the window
(`first_reject=...`); a dedup store closing at shutdown logs the evictions it had not yet reported.

Metrics: setting `METRICS_AUTH_TOKEN` (or `METRICS_AUTH_TOKEN_FILE`) makes the bridge serve `GET /metrics` in
Prometheus text format. The read-only token opens `/metrics` and nothing else, and the webhook credentials never
do. Without the token there is no endpoint.

The base image is rebuilt on a newer pinned digest (`ubi9/ubi-minimal@sha256:d8162789d7913ef8941481687b92ab4bbec311870948f66cf52b9cc7e0c57c06`) that carries the fixed
`krb5-libs` (no fixable HIGH or CRITICAL findings in the image scan).

Documentation: `README.md` now has a documentation map, a quick start, and an Operate and maintain section
(health checks, backup, upgrade and rollback); `INSTALLATION.md` covers install only, with the state volume, log rotation and the
optional `/metrics` token in the one `docker run`; the new `REFERENCE.md` holds the extended settings, spool and
logging detail and the metrics series. Certificates from a private CA or self-signed are now stated up front.

Spool hardening: a malformed stored alarm can no longer stall a destination's queue; a second damaged-spool
quarantine in the same second keeps the first file (`...corrupt-<UTC time>-2`); rate-limited events now report
their held-back count when the window ends; a failing summary observer logs `event=observer_error`; re-POSTs
of an alarm one destination cannot map are counted as duplicates; a timeout while sending a trap now counts as a
failed send (the alarm stays in the spool) instead of a delivery; a stored alarm that is not valid JSON is dropped
and counted like any other unmappable alarm; a dedup store that dropped to memory after a disk or database
error now returns to its state directory on its own (about every ten minutes) instead of staying in memory until
restart; recording an alarm no longer counts the whole table on every webhook; the TCP syslog forwarder replaces
carriage returns and NUL bytes, not only newlines, so a logged value cannot split or cut a record; the per-webhook
backlog check is a single index lookup instead of a row count, and the pre-spooling dedup lookup runs once per
webhook instead of once per destination; the container's TLS check now says whether the certificate directory is
missing, cannot be entered by the container user, or a file is missing or unreadable, and prints the uid and the
modes and owners it sees; the installation guide documents the directory-mode requirement and a one-line check.

Alarm spool, per-destination delivery and destination health. A trap destination that is down no longer
makes AppNeta retry in a loop.

- **Alarm spool:** when a destination is down the bridge stores each distinct alarm on disk and answers
  AppNeta `200`, then delivers the stored alarms to that destination once it recovers: one copy per alarm
  per destination, oldest first, at up to `SPOOL_DRAIN_RATE` (default 20 traps/s for the whole bridge;
  each send waits 50 ms for an ICMP refusal, so about 20/s is also the practical maximum). Spooled alarms do not
  expire. Duplicates are removed before spooling.
- **Per-destination delivery:** with several destinations, one that is up gets the alarm immediately and
  one that is down gets it from the spool. Before, one accepting destination counted as success and the
  other's copy was lost.
- **Response codes:** `200` when the alarm was delivered, stored or already known; `503` only when the
  spool itself cannot accept it (AppNeta retries); `400` when nothing maps (unchanged). With spooling off
  (`DEDUP_STATE_DIR=` empty) a failed send still returns `502`, as before. The lock-wait timeout still
  returns `502`.
- **Destination health:** a destination is marked down after a failed send, an ICMP port-unreachable
  after a real send, or (optionally) a failed TCP probe (`TRAP_DESTINATION_<N>_PROBE_TCP_PORT`). While it
  is down an active UDP probe runs every `DEST_PROBE_INTERVAL_SECONDS` (default 10) and it is up again
  after `DEST_UP_AFTER_PASSES` passes (default 2). A destination that silently drops UDP cannot be
  detected: the sends look like success.
- **New log events:** `dest_down`, `dest_still_down` (every `DEST_DOWN_REMINDER_MINUTES`, default 15),
  `dest_up`, `dest_drained` (when a recovered destination's stored alarms have all been sent),
  `spool_evicting`, `spool_error` (at most once a minute), `dropped_unsendable`, `drain_error` and
  `state_dir` at start. The
  `event=dest` line gains `state spooled oldest_age_s evicted drained` and `event=summary` gains
  `spool_bytes spool_errors`.
  A destination that recovers clears its failing state at once (`dest_state ... state=OK` and
  `last=ok`), and a destination that is down or holding stored alarms keeps appearing in the 60 s
  summary even when no alarms arrive.
- **Bounded disk use:** `SPOOL_MAX_MB` (default 256, minimum 16) caps the spool file, `SPOOL_FREE_RESERVE_MB`
  (default 128) leaves free space on the volume. At the cap the oldest alarms are dropped; dropping a
  RAISED alarm also drops its stored CLEARED alarm.
- **Behavior change: the image now declares the state directory as a volume** and sets
  `DEDUP_STATE_DIR=/var/lib/appneta-ia-bridge`, so spooling is on by default. Use a named volume
  (`-v ia-bridge-state:/var/lib/appneta-ia-bridge`) or a host directory owned by uid 10001. The default
  anonymous volume is orphaned by `docker rm` and a recreated container starts empty, so spooled alarms
  and duplicate-suppression history are not carried over. If the directory is not writable the container
  refuses to start, naming the path and uid 10001. Set `DEDUP_STATE_DIR=` to turn spooling off. A
  damaged spool file does not stop the container: it is renamed to `spool.sqlite3.corrupt-<UTC time>`
  (not deleted automatically), an empty spool is started and `spool_error` names the renamed file.
- **Oversized alarms:** an alarm whose trap does not fit in one UDP datagram is dropped for that
  destination (`dropped_unsendable`) instead of marking the destination down; `400` if no destination
  can take it. Before, it marked the destination down on every attempt.
- **Upgrade note:** dedup records written with spooling include the destination. Turning spooling off
  later can resend alarms that were handled inside the `DEDUP_TTL_HOURS` window. After upgrading, an
  AppNeta replay of an alarm already delivered is not re-sent.
- `LOG_SYSLOG_ADDRESS` accepts a comma-separated list of `host:port` targets; each has its own queue and
  thread, so a dead collector stalls only itself.
- `TRAP_DESTINATION_<N>_NAME` labels a destination (letters, digits, `_`, `.`, `-`, up to 64 characters,
  unique); the default label is `slot<N>`.
- The bridge now handles `SIGTERM`: it flushes the final summary and closes its state files within about
  6 to 9 seconds, inside Docker's default 10 second stop grace.
- `INSTALLATION.md` documents the state volume options, log rotation
  (`--log-opt max-size=10m --log-opt max-file=3`), the disk-cap layers and a compose example.

## v0.3.0

Red Hat UBI 9 base image, mandatory webhook authentication, duplicate suppression, new logging (periodic summary lines, `LOG_LEVEL`, optional syslog forwarding), and installation-guide fixes.

- **Base image change:** the container is now built on Red Hat UBI 9 (`ubi9/ubi-minimal`) instead of
  Debian. Behavior is unchanged: same ports, same environment variables, same TLS and plain modes.
  The image is about 22 MB larger.
- **Breaking: `AUTH_TYPE=NONE` is removed.** The container refuses to start without `AUTH_TYPE` set to
  `BEARER`, `BASIC` or `HEADER`. If you ran with `NONE`, configure authentication on the AppNeta
  connector and set the matching `AUTH_TYPE` and credential before upgrading.
- **Breaking: the SNMP community no longer defaults to `public`.** Set `TRAP_DESTINATION_<N>_COMMUNITY`
  or `TRAP_DESTINATION_<N>_COMMUNITY_FILE` for every destination; the container refuses to start and
  names the missing variable. `_FILE` reads the community from a mounted secret, like `AUTH_TOKEN_FILE`.
- **Breaking: the container now runs as non-root (uid 10001) and nginx listens on 8443, not 443.**
  Publish it with `-p 443:8443` (the connector URL does not change). Files you mount (`key.pem`,
  the token and community files) must be readable by group 10001: `chgrp 10001` and `chmod 640`, as
  `INSTALLATION.md` steps 2 and 3 show. A root-only `600` file now fails with a permission error at
  start. Compliant with Pod Security `restricted`.
- `alarm_bridge` now binds to loopback inside the image (`LISTEN_HOST=127.0.0.1`), so port 8080 is no
  longer reachable around nginx and TLS. To debug it directly, run with `-e LISTEN_HOST=0.0.0.0`.
  Outside the image the default is also loopback now (it was all interfaces).
- A trap community that is not valid UTF-8 is now rejected at startup with a clear message; before, it
  was sent with the bad byte replaced by `?`, so the receiver silently dropped the traps.
- New `AUTH_TYPE=HEADER` with `AUTH_HEADER_NAME`: checks one named request header against
  `AUTH_TOKEN`, for the AppNeta connector's custom `headers` list or a proxy that uses `Authorization`.
- `INSTALLATION.md`: `TRAP_DESTINATION_<N>_HOST` is now defined (the SpectroSERVER host, or the Trap
  Director server in a distributed SpectroSERVER setup; the `sm_trapd` host for Smarts), and step 5
  copies the bundle to the destination server before the install commands.
- **Duplicate suppression:** AppNeta can deliver one alarm event many times. The bridge now forwards each
  distinct event (alarm id, state, timestamp) once and answers repeats with `200` and no trap, so the
  receiver does not see a trap storm. An event is remembered only after a destination accepted it, so a
  failed send is still retried. New `DEDUP_STATE_DIR` (optional volume at `/var/lib/appneta-ia-bridge`,
  owned by uid 10001 in the image), `DEDUP_TTL_HOURS` (default 96) and `DEDUP_MAX_ENTRIES` (default
  100000). Without a volume the memory is lost on restart and a warning is logged. If the store fails
  the bridge keeps forwarding without suppression. A request that waits 10 seconds behind an identical
  in-flight event returns `502` so AppNeta retries. Run one container.
- **Failure visibility:** a one-line summary every 60 seconds (received, delivered, duplicates,
  undedupable, rejected, send failures), per-destination status, startup destination resolution
  checks, destination OK/FAILING state changes, and a warning on a high duplicate rate. Send failures
  no longer log a traceback and per-request lines are `DEBUG`, so a failing destination can no longer
  fill the disk. New `LOG_LEVEL` (default `INFO`). nginx no longer writes an access log.
- **Optional log forwarding:** `LOG_SYSLOG_ADDRESS=host:port` and `LOG_SYSLOG_PROTOCOL=udp|tcp` forward
  the bridge's log lines to a syslog collector (tag `appneta-ia-bridge`). Off by default; never blocks
  requests. UDP gives no delivery feedback. See `INSTALLATION.md`.

## v0.2.3

Documentation for how Smarts uses `SOURCE_ADDRESS`.

- `INSTALLATION.md` step 4 and `smarts-install/README.md`: Smarts attaches each alarm to the Host
  element matching the source address (an existing Host if the IP resolves to one, otherwise a new
  Host named after the IP), and how to check it with `dmctl`. Documentation only; no behavior change.

## v0.2.2

Troubleshooting additions to the installation guide.

- `INSTALLATION.md` step 7: `tcpdump` commands for the Docker host and the receiver, how to read the
  result, and `ss` and `nc` checks for the trap listener. Documentation only; no behavior change.

## v0.2.1

Organization id OID fix, MIB lint cleanup and a reworked installation guide.

- **Destination-side change (re-install required):** the organization id in each trap now uses
  `APPNETA-MIB::orgId` (`1.3.6.1.4.1.19041.2.1.5.1.5`), the same object AppNeta's native traps carry,
  instead of the bridge's own copy at `1.3.6.1.4.1.19041.2.1.5.10.2`. The value is unchanged. Run
  `./intelligent_alarms.sh --spectroserver uninstall` (or `--smarts`) and install again so the alarm
  mapping picks up the new OID; any custom rule that matched the old OID must be updated.
- `APPNETA-INTELLIGENT-ALARM-MIB` no longer defines its own `orgId` (it imports the APPNETA-MIB one)
  and now has an object group and compliance statement, so it compiles without `smilint` warnings.
- `INSTALLATION.md` reworked: the self-signed certificate link leads step 2; the token is shown where
  you use it (step 6); step 4 explains per-destination source addresses with a Spectrum VNA plus Smarts
  example; step 5 lists the split Spectrum install first and covers MIB compilation by the installer,
  copying the MIBs to other receivers, and that Smarts needs no MIB files; step 6 explains
  `ignoreSSLErrors` for CA-issued and self-signed certificates and adds a connector example that
  excludes Windows and macOS monitoring points; step 7 shows the bridge log line that confirms AppNeta
  is delivering and how to read a failed-trap warning.

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
