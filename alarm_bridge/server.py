"""HTTP webhook listener wiring auth, alarm mapping, and trap sending together."""

import json
import logging
import os
import shutil
import signal
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, NamedTuple, Optional

from alarm_bridge import drain, mapping
from alarm_bridge.auth import AuthError, validate_auth
from alarm_bridge.dedup import DedupStore, KeyedLocks, dedup_key
from alarm_bridge.drain import Drainer
from alarm_bridge.health import DestinationHealth
from alarm_bridge.metrics import render as render_metrics
from alarm_bridge.obs import RateLimitedEvent, Stats
from alarm_bridge.snmp_trap import UnsendableTrap
from alarm_bridge.spool import SpoolError, SpoolStore, check_state_dir, spool_error_count

logger = logging.getLogger("alarm_bridge")

# Defense in depth: nginx already caps bodies at 1m, but :8080 is also EXPOSEd.
MAX_BODY_BYTES = 1024 * 1024

# How long a copy waits for the in-flight request holding its key before giving up with a 502.
LOCK_WAIT_SECONDS = 10


def _spool_read(default, read, *args):
    """Return ``read(*args)``, or ``default`` when the spool read fails (the spool logs it)."""
    try:
        return read(*args)
    except SpoolError:
        return default


def make_handler(config, trap_sender, store=None, stats=None, locks=None, spool=None, health=None):
    unsendable = RateLimitedEvent("dropped_unsendable")
    if store is not None and locks is None:
        locks = KeyedLocks()

    class WebhookHandler(BaseHTTPRequestHandler):
        # Per-connection socket timeout: a client that stalls mid-request
        # (slowloris) can't pin a thread forever.
        timeout = 15

        def log_message(self, fmt, *args):
            logger.debug("%s - %s", self.address_string(), fmt % args)

        def do_GET(self):
            try:
                self._handle_get()
            except Exception:
                logger.error("Unhandled error while serving a GET request", exc_info=True)
                self.send_response(500)
                self.end_headers()

        def _handle_get(self):
            if not (config.metrics_auth_token and stats is not None and self.path.split("?", 1)[0] == "/metrics"):
                self.send_response(404)
                self.end_headers()
                return
            try:
                # read-only token: bearer only, the webhook's credentials never open /metrics
                validate_auth(self.headers, "BEARER", token=config.metrics_auth_token)
            except AuthError:  # own counter: auth_failures_total is the webhook's
                stats.metrics_auth_failed()
                self.send_response(401)
                self.end_headers()
                return
            body = render_metrics(stats.metrics()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            # Last-resort net: an unexpected bug must still answer the request. Without it the
            # connection is dropped, nginx returns 502, and AppNeta retries the poison payload.
            try:
                self._handle_post()
            except Exception:
                logger.error("Unhandled error while handling webhook request", exc_info=True)
                self.send_response(500)
                self.end_headers()

        def _handle_post(self):
            self._reject_reason = None  # first mapper error of this request; the handler outlives requests
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                content_length = -1
            if content_length < 0:
                if stats is not None:
                    stats.rejected("bad_content_length")
                self.send_response(400)
                self.end_headers()
                return
            if content_length > MAX_BODY_BYTES:
                if stats is not None:
                    stats.rejected("body_too_large")
                self.send_response(413)
                self.end_headers()
                return

            try:
                validate_auth(
                    self.headers,
                    config.auth_type,
                    username=config.auth_username,
                    password=config.auth_password,
                    token=config.auth_token,
                    header_name=config.auth_header_name,
                )
            except AuthError:
                if stats is not None:
                    stats.auth_failed()
                self.send_response(401)
                self.end_headers()
                return

            body = self.rfile.read(content_length) if content_length else b""
            try:
                payload = json.loads(body)
            except (ValueError, RecursionError):
                # ValueError covers JSONDecodeError, UnicodeDecodeError (bad UTF-8) and Python's
                # 4300-digit int limit; RecursionError covers pathological nesting.
                if stats is not None:
                    stats.rejected("invalid_json")
                self.send_response(400)
                self.end_headers()
                return

            if stats is not None:
                stats.received()
            key = None
            if store is not None:
                key = dedup_key(payload)
                if key is None and stats is not None:
                    stats.undedupable()
            # locks is never None when store is set; the check narrows the type for pyright.
            if spool is not None and (store is None or locks is None or key is None):
                status = self._deliver(spool, payload, None)
            elif store is None or locks is None or key is None:
                status = self._send_all(payload)
            elif not locks.acquire(key, LOCK_WAIT_SECONDS):
                # A same-key copy has held the lock for LOCK_WAIT_SECONDS (a slow send). If it fails, a 200 here
                # would lose the event, so 502 makes AppNeta retry; if it succeeded meanwhile, the
                # retry is a duplicate. Not held: no release.
                if stats is not None:
                    stats.inflight_timeout()
                status = 502
            else:
                try:
                    if spool is not None:
                        status = self._deliver(spool, payload, key)
                    elif store.seen(key):
                        if stats is not None:
                            stats.duplicate()
                        status = 200
                    else:
                        status = self._send_all(payload)
                        if status == 200:
                            # Only after a destination accepted: an all-failed 502 must stay retryable.
                            store.record(key)
                finally:
                    locks.release(key)

            if status == 400 and stats is not None:
                stats.rejected(self._reject_reason or "unmappable")
            self.send_response(status)
            self.end_headers()

        def _send_all(self, payload):
            """Map and send to every destination; return the HTTP status for the outcome."""
            trap_by_scheme = {}
            sent = False
            attempted = False
            for dest in config.trap_destinations:
                try:
                    trap_oid, send_varbinds = mapping.map_for_destination(
                        payload, dest, config.v3_id_offset, trap_by_scheme
                    )
                except mapping.AlarmMappingError as exc:
                    self._reject_reason = self._reject_reason or str(exc)
                    continue
                try:
                    trap_sender(dest.host, dest.port, dest.community, trap_oid, send_varbinds)
                    attempted = sent = True
                    if stats is not None:
                        stats.send_ok(f"{dest.host}:{dest.port}")
                except UnsendableTrap as exc:  # the alarm's fault: like an unmappable payload
                    self._reject_reason = self._reject_reason or f"unsendable: {exc}"
                    unsendable.note(dest.dest_id, str(exc))
                except Exception as exc:
                    attempted = True
                    reason = f"{type(exc).__name__}: {exc}"
                    logger.debug("Failed to send trap to %s:%s: %s", dest.host, dest.port, reason)
                    if stats is not None:
                        stats.send_failed(f"{dest.host}:{dest.port}", reason)

            if not sent:
                # 400: nothing was mappable, so the payload is bad and a retry won't help.
                # 502: we tried and every send failed; that is on our side, so let the sender retry.
                return 502 if attempted else 400
            if stats is not None:
                stats.delivered()
            return 200

        def _deliver(self, spool, payload, key):
            """Per destination: skip if owned, else send when up with no backlog, else spool.

            ``key`` is the dedup key, or None for an undedupable payload, which gets a unique spool
            key and is never recorded. The caller holds the per-key lock for a dedupable payload.
            Returns 503 if any spool write failed, 400 if nothing mapped, otherwise 200.
            """
            if key is None:
                alarm_id = state = ""
                key_json = json.dumps([drain.UNDEDUPABLE, uuid.uuid4().hex])
            else:
                alarm_id, state = key[0], key[1]
                key_json = json.dumps(list(key))
            trap_by_scheme = {}
            # 3-part record from before spooling: the same for every destination, so look it up once.
            legacy_seen = key is not None and store is not None and store.seen(key)
            mapped_any = spool_failed = sent = spooled = False
            skipped = dropped = unmapped = 0
            for dest in config.trap_destinations:
                dest_id = dest.dest_id
                dest_key = None if key is None else (*key, dest_id)
                # A failed spool read (already logged by the spool) reads as "not evicted" and
                # "no backlog": a broken spool must not stop live delivery to an up destination.
                if dest_key is not None and store is not None and (
                    store.seen(dest_key)
                    or legacy_seen
                    or (state == "CLEARED" and _spool_read(False, spool.raise_evicted, dest_id, alarm_id))
                ):
                    skipped += 1
                    continue
                try:
                    trap_oid, varbinds = mapping.map_for_destination(
                        payload, dest, config.v3_id_offset, trap_by_scheme
                    )
                except mapping.AlarmMappingError as exc:
                    self._reject_reason = self._reject_reason or str(exc)
                    unmapped += 1
                    continue
                mapped_any = True
                if (health is None or health.is_up(dest_id)) and not _spool_read(False, spool.has_backlog, dest_id):
                    if dest_key is not None and store is not None and store.seen(dest_key):
                        # The drainer sent and recorded it after the check above (it records before
                        # it deletes, so an empty backlog means the record is visible now).
                        skipped += 1
                        continue
                    outcome = self._send_one(dest, trap_oid, varbinds)
                    if outcome != "failed":  # sent, or unsendable (dropped, never spooled)
                        if outcome == "sent":
                            sent = True
                        else:
                            dropped += 1
                        if dest_key is not None and store is not None:
                            store.record(dest_key)
                        continue
                try:
                    if spool.enqueue(key_json, dest_id, payload, alarm_id, state) == "duplicate":
                        skipped += 1  # already spooled: a re-POST
                    else:
                        spooled = True
                except SpoolError:
                    # Already logged (rate limited) by the spool; keep going so the other
                    # destinations still get this alarm, then answer 503 so AppNeta retries.
                    spool_failed = True
            owned = skipped > 0 and skipped + unmapped == len(config.trap_destinations)
            if owned and stats is not None:
                stats.duplicate()
            if sent and stats is not None:
                stats.delivered()
            if spool_failed:
                return 503
            if not mapped_any and not skipped:
                return 400
            if dropped and not (sent or spooled or skipped):
                return 400  # no destination could take it: as if nothing mapped
            return 200

        def _send_one(self, dest, trap_oid, varbinds):
            """Send one trap: ``"sent"``, ``"unsendable"`` (the alarm cannot be sent; the destination
            stays up), or ``"failed"`` (the destination is marked down)."""
            target = f"{dest.host}:{dest.port}"
            try:
                trap_sender(dest.host, dest.port, dest.community, trap_oid, varbinds)
            except UnsendableTrap as exc:
                self._reject_reason = self._reject_reason or f"unsendable: {exc}"
                unsendable.note(dest.dest_id, str(exc))
                return "unsendable"
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                logger.debug("Failed to send trap to %s: %s", target, reason)
                if stats is not None:
                    stats.send_failed(target, reason)
                if health is not None:
                    health.mark_down(dest.dest_id, reason)
                return "failed"
            if stats is not None:
                stats.send_ok(target)
            return "sent"

    return WebhookHandler


MB = 1024 * 1024
THREAD_STOP_SECONDS = 3.0  # shared budget for joining the drainer, prober and summary threads


class Spooling(NamedTuple):
    spool: SpoolStore
    health: DestinationHealth
    drainer: Drainer
    stats_fn: Callable  # stats_fn(dest_id) -> spool/drain counters; stats_fn(None) -> spool totals


def _log_state_dir(state_dir, disk_usage=shutil.disk_usage, ismount=os.path.ismount) -> None:
    """One startup line: where the state lives, whether it is a mount point, and its free space."""
    try:
        free_mb = str(disk_usage(state_dir).free // MB)
    except OSError:
        free_mb = "unknown"
    if ismount(state_dir):
        logger.info("event=state_dir path=%s mount_point=yes free_mb=%s", state_dir, free_mb)
    else:
        logger.warning(
            "event=state_dir path=%s mount_point=no free_mb=%s (not a mount point itself: unless a parent "
            "directory is a mounted volume, the spool and dedup state are lost when the container is removed)",
            state_dir, free_mb,
        )


def _evict_recorder(store):
    """on_evict callback: an evicted alarm counts as owned for its destination, so a replay is not re-spooled."""
    def on_evict(batch):
        for key_json, dest_id in batch:
            key = tuple(json.loads(key_json))
            if key and key[0] == drain.UNDEDUPABLE:
                continue
            store.record((*key, dest_id))

    return on_evict


def _build_spooling(config, store, trap_sender) -> Spooling:
    """Open the spool and build the prober and drainer (not started). Raises SpoolError."""
    spool = SpoolStore(
        config.dedup_state_dir, config.spool_max_mb * MB, config.spool_free_reserve_mb * MB,
        on_evict=_evict_recorder(store),
    )
    drainer = None  # late bound: health needs stats_fn, stats_fn needs the drainer, the drainer needs health

    def stats_fn(dest_id):
        if dest_id is None:
            return {"bytes": spool.bytes_used(), "errors": spool_error_count()}
        return {
            "depth": spool.depth(dest_id),
            "oldest_age": spool.oldest_age(dest_id),
            "evicted": spool.counters(dest_id)["evicted"],
            "drained": 0 if drainer is None else drainer.drained(dest_id),
        }

    try:
        health = DestinationHealth(
            config.trap_destinations, config.probe_interval_seconds, config.up_after_passes,
            config.reminder_minutes * 60, stats_fn,
        )
        drainer = Drainer(config, spool, health, store, trap_sender, config.spool_drain_rate)
        dropped = spool.drop_orphans({d.dest_id for d in config.trap_destinations})
    except BaseException:
        spool.close()
        raise
    if dropped:
        logger.warning("event=spool_orphans_dropped count=%d (rows for destinations no longer configured)", dropped)
    return Spooling(spool, health, drainer, stats_fn)


def _stats_observers(destinations, health, stats_fn):
    """Stats keys destinations by host:port; map each name to its dest_ids (two slots may share one)."""
    ids = {}
    for dest in destinations:
        ids.setdefault(f"{dest.host}:{dest.port}", []).append(dest.dest_id)

    def spool_fn(name):
        if name is None:
            return stats_fn(None)
        total = {"depth": 0, "oldest_age": 0, "evicted": 0, "drained": 0}
        for dest_id in ids.get(name, ()):
            one = stats_fn(dest_id)
            for field in ("depth", "evicted", "drained"):
                total[field] += one.get(field, 0)
            total["oldest_age"] = max(total["oldest_age"], one.get("oldest_age", 0))
        return total

    def state_fn(name):
        return all(health.is_up(dest_id) for dest_id in ids.get(name, ()))

    return spool_fn, state_fn


def _install_sigterm(server):
    """Make SIGTERM (docker stop) end serve_forever so run_server's teardown runs; return a restore function.

    Only the main thread may set signal handlers, so elsewhere (tests) this does nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    def on_sigterm(_signum, _frame):
        # shutdown() waits for serve_forever, which runs in this thread, so call it from another one.
        # It does not wait for in-flight requests: their threads are daemons.
        threading.Thread(target=server.shutdown, name="sigterm-shutdown", daemon=True).start()

    previous = signal.signal(signal.SIGTERM, on_sigterm)
    return lambda: signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)


def _stop_all(workers, budget, clock=time.monotonic) -> None:
    """Signal every worker to stop, then join them against one shared deadline.

    A worker wedged mid-send or in DNS must not delay the summary flush and store close past
    Docker's 10 s stop grace (SIGKILL): THREAD_STOP_SECONDS here plus SYSLOG_DRAIN_SECONDS in cli.
    """
    for worker in workers:
        worker.stop(timeout=0)
    deadline = clock() + budget
    for worker in workers:
        worker.stop(timeout=max(0.0, deadline - clock()))


def run_server(config, trap_sender) -> Optional[str]:
    """Serve until SIGTERM or KeyboardInterrupt, then tear down; None after a normal shutdown.

    Returns an error message, without serving, when spooling is on and the state directory or the
    spool in it is unusable.
    """
    if config.spool_enabled:
        try:
            check_state_dir(config.dedup_state_dir)
        except SpoolError as exc:
            logger.error("event=startup_refused error=%s", exc)
            return str(exc)
        _log_state_dir(config.dedup_state_dir)
    store = DedupStore(config.dedup_state_dir, config.dedup_ttl_hours, config.dedup_max_entries)
    spooling = None
    if config.spool_enabled:
        try:
            spooling = _build_spooling(config, store, trap_sender)
        except SpoolError as exc:
            store.close()
            message = f"spool in state directory {config.dedup_state_dir} could not be opened: {exc}"
            logger.error("event=startup_refused error=%s", message)
            return message
    spool_fn = state_fn = None
    if spooling is not None:
        spool_fn, state_fn = _stats_observers(config.trap_destinations, spooling.health, spooling.stats_fn)
    stats = Stats(
        [f"{d.host}:{d.port}" for d in config.trap_destinations], lambda: store.mode,
        spool_fn=spool_fn, state_fn=state_fn,
    )
    if spooling is not None and state_fn is not None:
        names = {d.dest_id: f"{d.host}:{d.port}" for d in config.trap_destinations}

        def _on_up(dest_id):
            if state_fn(names[dest_id]):  # a shared host:port is recovered only when every slot is up
                stats.recovered(names[dest_id])

        spooling.health.on_up = _on_up
    server = None
    restore_sigterm = None
    try:
        stats.check_destinations(config.trap_destinations)
        if not config.dedup_state_dir:
            logger.warning(
                "event=dedup_store mode=memory persistent=no "
                "(set DEDUP_STATE_DIR to a mounted volume to keep dedup state across restarts)"
            )
            logger.warning(
                "event=spool mode=off (set DEDUP_STATE_DIR to a mounted volume to hold alarms while a "
                "destination is down; without it a failed send returns 502)"
            )
        handler = make_handler(
            config, trap_sender, store=store, stats=stats,
            spool=None if spooling is None else spooling.spool,
            health=None if spooling is None else spooling.health,
        )
        server = ThreadingHTTPServer((config.listen_host, config.listen_port), handler)
        restore_sigterm = _install_sigterm(server)
        logger.info("appneta-ia-bridge listening on port %s", config.listen_port)
        if config.listen_host != "0.0.0.0":  # noqa: S104 - comparison only, not a bind
            logger.info("bound to %s only", config.listen_host)
        stats.start()
        if spooling is not None:
            spooling.health.start()
            spooling.drainer.start()
        server.serve_forever()
    finally:
        if restore_sigterm is not None:
            restore_sigterm()
        if server is not None:
            server.server_close()  # stop accepting before the joins
        workers = [stats] if spooling is None else [spooling.drainer, spooling.health, stats]
        _stop_all(workers, THREAD_STOP_SECONDS)
        stats.flush()  # before the spool closes: the summary reads spool counters
        store.close()
        if spooling is not None:
            spooling.spool.close()
    return None
