# Reference

Settings, behavior and monitoring detail for a running bridge. You do not need any of this to install:
[`INSTALLATION.md`](INSTALLATION.md) gets a working bridge with the defaults. Come here to tune a setting,
understand a log line, or set up monitoring. The core settings (destinations, authentication, TLS) are in
[`README.md`](README.md#configuration).

- [Extended settings](#extended-settings)
- [State volume and the alarm spool](#state-volume-and-the-alarm-spool)
- [When a destination is down](#when-a-destination-is-down)
- [Duplicate suppression](#duplicate-suppression)
- [Logging](#logging)
- [Metrics for Prometheus and Grafana](#metrics-for-prometheus-and-grafana)
- [Compose example](#compose-example)
- [Stopping the container](#stopping-the-container)

## Extended settings

Every setting below has a default that works. Change one only for a reason you can name.

| Variable | Default | Meaning |
|---|---|---|
| `DEDUP_STATE_DIR` | `/var/lib/appneta-ia-bridge` (set by the image) | State directory; empty turns the spool and persistent duplicate suppression off |
| `DEDUP_TTL_HOURS` | `96` | How long an alarm event is remembered (integer, 1 or more) |
| `DEDUP_MAX_ENTRIES` | `100000` | Oldest entries are dropped beyond this (100 or more) |
| `SPOOL_MAX_MB` | `256` | Hard ceiling on the spool file (16 or more) |
| `SPOOL_FREE_RESERVE_MB` | `128` | Free space to leave on the state filesystem (0 or more) |
| `SPOOL_DRAIN_RATE` | `20` | Upper limit on stored traps sent per second, shared by all destinations (1 to 1000). Each successful send waits 50 ms for an ICMP refusal, so the real rate is at most about 20 per second |
| `DEST_PROBE_INTERVAL_SECONDS` | `10` | How often a down destination is probed (2 to 300) |
| `DEST_UP_AFTER_PASSES` | `2` | Consecutive passing probes before a destination counts as up (1 to 10) |
| `DEST_DOWN_REMINDER_MINUTES` | `15` | Interval of the `dest_still_down` reminder (1 to 1440) |
| `TRAP_DESTINATION_<N>_NAME` | `slot<N>` | Label for the destination in logs and the spool: 1 to 64 characters of letters, digits, `_`, `.`, `-`, not `.` or `..`, and unique. An unnamed destination is identified by its slot number, so reordering destinations or removing one from the middle makes stored alarms and duplicate records follow the slot to a different host. Set `NAME` on every destination before relying on the spool |
| `TRAP_DESTINATION_<N>_PROBE_TCP_PORT` | unset (off) | Optional TCP port to probe on the destination host (1 to 65535) |
| `LOG_LEVEL` | `INFO` | `DEBUG` adds a line per request |
| `LOG_SYSLOG_ADDRESS` | unset (off) | `host:port`, or a comma-separated list, to also forward log lines to syslog collectors |
| `LOG_SYSLOG_PROTOCOL` | `udp` | `udp` or `tcp` (TCP lines are newline-terminated); applies to every target |
| `METRICS_AUTH_TOKEN` / `METRICS_AUTH_TOKEN_FILE` | unset (`/metrics` off) | Read-only bearer token for `/metrics` only; must differ from `AUTH_TOKEN` |

## State volume and the alarm spool

The bridge keeps two things in its state directory, `/var/lib/appneta-ia-bridge`:

- **Duplicate-suppression memory** (see [below](#duplicate-suppression)).
- **The alarm spool.** When a trap destination is down, the bridge stores each distinct alarm on disk, answers
  AppNeta `200` so AppNeta stops retrying, and delivers the stored alarms when the destination comes back.

The image declares the directory as a volume and sets `DEDUP_STATE_DIR` to it, so spooling is on by default.
Where the volume comes from decides whether that state survives, so choose it deliberately. The install uses a
**named volume**, which is the recommended setup:

| Option | Survives `docker stop`, `start`, `restart` | Survives `docker rm` and a new `docker run` (an upgrade) | Notes |
|---|---|---|---|
| Named volume `ia-bridge-state` (recommended) | yes | yes, the new container mounts the same volume | Created on first use. It inherits the image's ownership of the directory (uid 10001) and works as is. |
| Host directory, `-v /srv/ia-bridge-state:/var/lib/appneta-ia-bridge` | yes | yes | Run `mkdir -p /srv/ia-bridge-state && chown 10001 /srv/ia-bridge-state` first. A bind mount keeps the host directory's ownership; the bridge runs as uid 10001 and refuses to start if it cannot write there. |
| Anonymous volume (what you get with no `-v` at all) | yes | **no** | See below. |

An anonymous volume survives the container being stopped and started again, but `docker rm` orphans it, and a
container you create afterwards gets a **new, empty** one. Spooled alarms and duplicate history are not reused,
so upgrading by removing and recreating the container while a destination is down would silently lose the
spool. At start the bridge logs whether the state directory is a mount point and how much space is free
(`event=state_dir path=... mount_point=yes|no free_mb=...`); it cannot tell an anonymous volume from a named
one. Run a single container: the state files are not shared safely between replicas.

If the state directory is missing or not writable by uid 10001, the container **refuses to start** and the error
names the path and uid 10001. It does not fall back to memory, because that would lose the alarms the spool
exists to keep.

If the spool file itself is damaged, the bridge still starts: it renames the file (and its `-wal` and `-shm`
files) to `spool.sqlite3.corrupt-<UTC time>`, starts an empty spool and logs `event=spool_error` naming the
renamed file. The alarms in the damaged file are not delivered. The renamed files are not deleted
automatically; remove them when you no longer need them.

To run without the spool and without persistent duplicate state, set `DEDUP_STATE_DIR=` to an empty value (a
down destination then makes the bridge answer `502`). If you turn spooling off after running with it, alarms
already handled inside the `DEDUP_TTL_HOURS` window can be sent again.

### Disk use is bounded

Docker puts no size limit on a volume, so the bridge limits itself in layers:

1. **`SPOOL_MAX_MB`** (default `256`, minimum `16`) is a hard ceiling on the spool database file. SQLite's
   write-ahead journal is budgeted inside this cap.
2. **`SPOOL_FREE_RESERVE_MB`** (default `128`) keeps that much free space on the state directory's filesystem.
   The usable spool is the smaller of `SPOOL_MAX_MB` and the free space minus this reserve, so on a small disk
   the bridge drops its oldest spooled alarms rather than take the last of the space.
3. **Docker's own log** never rotates by default. The install adds `--log-opt max-size=10m --log-opt max-file=3`,
   which caps the container log at 30 MB.

When the spool is full the **oldest** alarms are dropped and the newest kept, and the bridge logs
`event=spool_evicting`. Eviction keeps raise and clear alarms consistent: dropping a stored RAISED alarm also
drops its stored CLEARED alarm, because a clear for an alarm the receiver never saw is noise. Dropping a
CLEARED alarm never drops anything else. A lone CLEARED alarm (its raise was already delivered) is dropped only
as a last resort. A CLEARED alarm that arrives after its raise was dropped is discarded. Spooled alarms never
expire by age, so after a long outage every stored alarm is still delivered.

## When a destination is down

Each alarm is handled **per destination**. With Spectrum and Smarts both configured and one down, the alarm goes
straight to the one that is up and is spooled for the other.

- Duplicates are removed **before** spooling, so an AppNeta retry storm stores each distinct alarm once.
- AppNeta gets `200` whenever the alarm was delivered, stored or already known. It gets `503` only when the
  spool itself cannot accept the alarm (disk error, volume gone), so AppNeta retries it, and `400` when the
  payload maps to nothing. With spooling off, `502` when every send failed. A request that times out waiting on
  an identical in-flight event also returns `502`.
- When the destination recovers, each stored alarm is sent **once**, oldest first, at up to `SPOOL_DRAIN_RATE`
  traps per second, so the receiver sees no burst. The limit is for the whole bridge, and in practice at most
  about 20 per second. A new alarm for a destination that still has a backlog is stored behind it to keep the
  order.
- An alarm too large to fit in one SNMP trap datagram is the alarm's problem, not the destination's: it is
  dropped for that destination with `event=dropped_unsendable`, the destination stays up, and the next alarms
  are delivered. If no destination can take it, AppNeta gets `400`.

**How the bridge decides a destination is down.** SNMP traps are unacknowledged UDP, so a send alone rarely
fails. The bridge combines:

- a failed send (for example a DNS failure);
- a **passive check** after real sends: if the host answers with an ICMP port-unreachable (the host is up but
  nothing listens on the trap port), the destination is marked down;
- an **active UDP probe**, run only while a destination is down, every `DEST_PROBE_INTERVAL_SECONDS`, to detect
  recovery. It is never sent to a healthy destination, because some receivers log every stray datagram as an
  error. The destination is up again after `DEST_UP_AFTER_PASSES` passing probes;
- an optional **TCP probe** (`TRAP_DESTINATION_<N>_PROBE_TCP_PORT`), checked every probe interval, that marks the
  destination down when the connection fails. Use a port served by the same process that receives traps (for
  SpectroSERVER, TCP 14002 is a reasonable choice). A Smarts broker port is a different process from `sm_trapd`,
  so a passing TCP probe there does not prove traps are received.

**Limit, stated plainly:** if a destination silently drops UDP (a dead host, or a firewall rule that drops
rather than rejects), no ICMP reply comes back and the send looks like a success. The bridge cannot detect that
case with UDP alone, and alarms sent before another check notices (the optional TCP probe narrows this) are
counted as delivered and may be lost. A passing probe means the port is listening, not that the receiver is
fully ready; after a SpectroSERVER restart the port binds before models finish loading.

**Delivery is at-least-once in one narrow case:** if the bridge crashes between sending a stored alarm and
deleting it, that one alarm is sent again after the restart.

## Duplicate suppression

AppNeta can deliver one alarm event many times. The bridge forwards each distinct event (alarm id, state and
timestamp) once and answers repeats with `200` and no trap, so the receiver does not see a trap storm. An event
is remembered only after a destination accepted it, so a failed send is still retried. `DEDUP_TTL_HOURS` and
`DEDUP_MAX_ENTRIES` bound the memory.

If the store fails, the bridge keeps forwarding alarms rather than dropping them: it logs an error and switches
to in-memory suppression (`dedup_store=memory-degraded`). It retries the state directory about every ten
minutes and returns to it, keeping the alarms it recorded meanwhile, once the directory is writable again
(`event=dedup_store_recovered`). A request that waits more than 10 seconds behind an identical in-flight event
gets `502` so AppNeta retries it.

## Logging

`docker logs appneta-ia-bridge` carries a one-line summary every 60 seconds while there is activity, one line per
destination, and the events below. It never logs alarm payloads or secrets. The nginx access log is off.
Destination lines in the summary are keyed by `host:port`, so two destination slots that share a host and port
are combined there; the events use the destination label (`NAME`, or `slot<N>`).

| Event | When | Fields |
|---|---|---|
| `event=summary` | every 60 s with activity | `window received delivered duplicates dup_pct undedupable rejected auth_failed send_failed inflight_timeouts dedup_store spool_bytes spool_errors` (plus `first_reject` when a payload was rejected) |
| `event=dest` | one line per destination with each summary | `dest ok failed last state spooled oldest_age_s evicted drained` (`state` is `up` or `down`) |
| `event=dest_down` | a destination goes down | `dest reason` |
| `event=dest_still_down` | every `DEST_DOWN_REMINDER_MINUTES` while down | `dest spooled oldest_age_s` |
| `event=dest_up` | a destination recovers | `dest outage_s` |
| `event=dest_drained` | once after each recovery, when that destination's stored alarms have all been sent | `dest drained evicted drain_s` |
| `event=spool_evicting` | the oldest alarms were dropped; at most once a minute | `reason` (`cap` or `disk`), `count pairs lone_raises lone_clears` |
| `event=spool_error` | the spool failed to read or write; at most once a minute | `count error` (HTTP clients get `503`) |
| `event=spool_error` (`quarantined=`) | at start, when a damaged spool file was renamed aside | `error quarantined` |
| `event=dropped_unsendable` | an alarm too large to send as one trap was dropped for a destination | `dest count reason` |
| `event=drain_error` | sending stored alarms to a destination failed unexpectedly; the others keep draining | `dest count reason` |
| `event=state_dir` | at start | `path mount_point free_mb` |

Alert on `event=spool_error` and `event=dest_down`. Treat a growing `oldest_age_s` or any `evicted` count above
zero as lost or about-to-be-lost alarms.

**Forwarding logs to a monitor.** Add `-e LOG_SYSLOG_ADDRESS=<collector-host>:<port>` (and
`-e LOG_SYSLOG_PROTOCOL=tcp` for a TCP listener). To forward to several collectors list them with commas,
`-e LOG_SYSLOG_ADDRESS=collector-a:514,collector-b:1514`; each target has its own queue and thread, so a dead
collector delays only itself. Lines are tagged `appneta-ia-bridge`. Forwarding is plaintext and
unauthenticated, so use it only on a network you trust. It never delays a request, and `docker logs` still shows
everything. UDP gives no delivery feedback, so a collector that is not listening usually looks like success;
after a forwarding failure the bridge drops further forwarded records for 10 seconds before trying again. Only
the bridge's own lines are forwarded, not nginx's. Alternatively leave `LOG_SYSLOG_ADDRESS` unset and use
Docker's own `--log-driver syslog --log-opt syslog-address=udp://<collector-host>:<port>`, which forwards
everything the container writes (on older Docker versions a non-default log driver can change how `docker logs`
behaves).

## Metrics for Prometheus and Grafana

`/metrics` is off until you set `METRICS_AUTH_TOKEN_FILE` (installation step 3 creates the token and step 4 passes
it). Without that token there is no endpoint (404). Only that read-only token opens `/metrics`, it opens nothing
else, and the webhook token never works there. Counters are totals since the container started, so use `rate()`
or `increase()` in Grafana. Without spooling there are no spool or destination-up series.

Check it without putting the token on a command line:

```bash
sudo sh -c 'printf "Authorization: Bearer %s\n" "$(cat /etc/appneta-ia-bridge/secrets/metrics_token)"' \
  | curl --cacert /etc/appneta-ia-bridge/tls/cert.pem -H @- https://ia-bridge.example.com/metrics
```

| Series (prefix `appneta_ia_bridge_`) | Type | Meaning |
|---|---|---|
| `webhooks_received_total`, `webhooks_delivered_total`, `webhooks_duplicates_total`, `webhooks_rejected_total`, `webhooks_undedupable_total` | counter | Webhook outcomes |
| `auth_failures_total` | counter | Webhook requests refused for bad credentials |
| `metrics_auth_failures_total` | counter | `/metrics` requests refused for bad credentials |
| `trap_sends_total{destination,result}` | counter | Send attempts, `result` is `ok` or `failed` |
| `destination_up{destination}` | gauge | 1 up, 0 down (spooling on) |
| `spool_depth{destination}`, `spool_oldest_age_seconds{destination}` | gauge | Queued alarms and the age of the oldest (spooling on) |
| `spool_evicted_total`, `spool_drained_total` | counter | Per destination (spooling on) |
| `dedup_store_mode{mode}` | gauge | 1 on the current mode: `sqlite`, `memory` or `memory-degraded` |

A scrape job with the read-only token (a bearer token whatever `AUTH_TYPE` is). Copy the token file to the
Prometheus host. If the bridge uses a certificate from a private or self-signed CA, give Prometheus that CA:

```yaml
scrape_configs:
  - job_name: appneta-ia-bridge
    scheme: https
    metrics_path: /metrics
    authorization:
      credentials_file: /etc/prometheus/ia-bridge-token
    tls_config:
      ca_file: /etc/prometheus/ia-bridge-ca.pem
    static_configs:
      - targets: ["ia-bridge.example.com:443"]
```

Useful alerts:

- `appneta_ia_bridge_destination_up == 0`
- `appneta_ia_bridge_spool_oldest_age_seconds > 600`
- `increase(appneta_ia_bridge_auth_failures_total[5m]) > 0`
- `increase(appneta_ia_bridge_metrics_auth_failures_total[5m]) > 0`
- `appneta_ia_bridge_dedup_store_mode{mode="memory-degraded"} == 1`
- `rate(appneta_ia_bridge_webhooks_received_total[15m]) == 0`: no webhooks arriving at all. The bridge starts and
  stays healthy whether or not AppNeta is sending, so this catches a broken connector, certificate or firewall.
  Choose a window that fits your alarm volume; a quiet site can go a long time without an alarm.

## Compose example

```yaml
services:
  appneta-ia-bridge:
    image: appneta-ia-bridge
    restart: unless-stopped
    stop_grace_period: 20s
    ports:
      - "443:8443"
    environment:
      TRAP_DESTINATION_1_HOST: spectrum.example.com
      TRAP_DESTINATION_1_COMMUNITY_FILE: /run/secrets/trap_community
      TRAP_DESTINATION_1_SCHEME: general
      TRAP_DESTINATION_1_SOURCE_ADDRESS: "<ip-of-appneta-model>"
      AUTH_TYPE: BEARER
      AUTH_TOKEN_FILE: /run/secrets/auth_token
      METRICS_AUTH_TOKEN_FILE: /run/secrets/metrics_token
    volumes:
      - /etc/appneta-ia-bridge/secrets:/run/secrets:ro
      - /etc/appneta-ia-bridge/tls:/etc/appneta-ia-bridge/tls:ro
      - ia-bridge-state:/var/lib/appneta-ia-bridge
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  ia-bridge-state:
```

## Choosing the trap source address

Every trap carries a source address, and Spectrum or Smarts uses it to decide which object the alarm
belongs to. By default that is the address the packet came from, which for a container is a private
address on the Docker network: the receiver does not know it, and it changes whenever the container is
recreated. `TRAP_DESTINATION_<N>_SOURCE_ADDRESS` makes the bridge state the address itself, as
`snmpTrapAddress.0`, so the receiver sees the same one on every rebuild.

Most installs use the Docker host's own address as both the model's address and `SOURCE_ADDRESS`; that is the simplest
setup. The example below uses two different addresses to show that the model's address is the one that counts.

Example: the bridge runs on a host at `192.0.2.20` and Spectrum has an `AppNetaAdmin` model whose
**Monitoring Point IP Address** is `192.0.2.50`. Set `-e TRAP_DESTINATION_1_SOURCE_ADDRESS=192.0.2.50`.
Every trap says it is about `192.0.2.50`, and the alarms land on that model. The value is the address of
the object you want the alarms on, not necessarily the bridge host's address; the host's address
(`192.0.2.20`) is right only if that is the address Spectrum or Smarts models for the bridge. An address
the receiver has no element for matches nothing, so check where the alarms land.

Spectrum matches a trap to a model by IP address (IP-header trap mapping, which the install turns on), so
the model's Monitoring Point IP Address and `SOURCE_ADDRESS` must be identical.

Smarts: `sm_trapd` attaches each notification to the `Host` element that matches the trap's source
address. If Smarts has a Host that resolves to that IP the alarms attach to it; otherwise Smarts creates
a `Host` named after the IP. With `none`, the packet's source IP (the bridge host) is used the same way.
This was verified on Smarts 25.4; it is observed behavior, not a documented setting. To see where alarms
land, open a notification in the Smarts console, or run
`dmctl -s INCHARGE-OI get ICS_Notification::<notification-name>` and read `ElementName` and `OccurredOn`.

## Compiling the MIBs by hand

You normally never do this: `intelligent_alarms.sh install` compiles both MIBs (`mibs/*.mib`) into
Spectrum's `MibDatabase` as part of the install, and on a split install `--oneclick install` does it on
the OneClick host. Compile by hand only if the install log shows the compile failed or was skipped on the
host that has `MibDatabase`. Put both files in one directory first, because `APPNETA-INTELLIGENT-ALARM-MIB`
imports `APPNETA-MIB`:

```bash
export SPECTRUM_HOME=<the directory the installer printed>
mkdir -p /tmp/mib-compile
cp /tmp/appneta-ia-bridge-install/mibs/*.mib /tmp/mib-compile/
bash "$SPECTRUM_HOME/MibDatabase/scripts/BulkMibCompile.sh" -d /tmp/mib-compile -f "*.mib"
```

## Stopping the container

The bridge handles `SIGTERM`: it stops accepting requests, stops the delivery threads, writes a final summary
line and closes its state files. Worst case this takes about 6 to 9 seconds, so Docker's default 10 second stop
grace is enough. `stop_grace_period: 20s` in the compose example (or `docker stop -t 20`) leaves margin on a slow
disk.
