"""Spool drainer: replays queued traps to each healthy destination, oldest first, rate limited."""

import json
import logging
import threading
import time

from alarm_bridge import mapping
from alarm_bridge.obs import RateLimitedEvent
from alarm_bridge.snmp_trap import UnsendableTrap

logger = logging.getLogger("alarm_bridge")

IDLE_POLL_SECONDS = 1.0
UNMAPPABLE_LOG_INTERVAL_SECONDS = 60
UNDEDUPABLE = "undedupable"


class Drainer:
    """One thread walks the configured destinations; a destination that is down is skipped."""

    def __init__(self, config, spool, health, store, trap_sender, rate_per_second, sleep=time.sleep,
                 clock=time.monotonic):
        self._config = config
        self._spool = spool
        self._health = health
        self._store = store
        self._sender = trap_sender
        self._rate = max(1, int(rate_per_second))
        self._sleep = sleep
        self._clock = clock
        self._idle = IDLE_POLL_SECONDS
        self._next_send = None  # monotonic time the next send may start (pacing)
        self._lock = threading.Lock()  # guards the counters only; never held across a send
        self._drained = {}
        self._pending_delete = {}  # dest_id -> rowid sent but not yet deleted; never resent
        self._unmappable = {}
        self._last_unmappable_log = None
        self._unmappable_since_log = 0
        self._unsendable = RateLimitedEvent("dropped_unsendable", clock=clock)
        self._errors = RateLimitedEvent("drain_error", clock=clock)
        self._stop = threading.Event()
        self._thread = None

    def drained(self, dest_id: str) -> int:
        with self._lock:
            return self._drained.get(dest_id, 0)

    def dropped_unmappable(self, dest_id: str) -> int:
        with self._lock:
            return self._unmappable.get(dest_id, 0)

    def dropped_unsendable(self, dest_id: str) -> int:
        return self._unsendable.total(dest_id)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="spool-drainer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._drain_pass() == 0:
                self._stop.wait(self._idle)

    def _drain_pass(self) -> int:
        """One round over the destinations. A destination that raises is logged (rate limited) and
        skipped, so it cannot stop the others."""
        sent = 0
        for dest in self._config.trap_destinations:
            if self._stop.is_set():
                break
            try:
                sent += self.drain_once(dest)
            except Exception as exc:
                self._errors.note(dest.dest_id, f"{type(exc).__name__}: {exc}")
        return sent

    def drain_once(self, dest) -> int:
        """Send up to one second's budget to ``dest``; return the number of traps sent."""
        dest_id = dest.dest_id
        sent = 0
        pending = self._pending_delete.get(dest_id)
        if pending is not None:
            # Delivered earlier but the delete failed: retry only the delete, never the send.
            self._spool.delete(pending)
            del self._pending_delete[dest_id]
        while sent < self._rate and not self._stop.is_set() and self._health.is_up(dest_id):
            row = self._spool.oldest(dest_id)
            if row is None:
                self._health.backlog_empty(dest_id)
                break
            rowid, key_json, payload = row
            try:
                trap_oid, varbinds = mapping.map_for_destination(payload, dest, self._config.v3_id_offset)
            except Exception:  # AlarmMappingError, or anything else a malformed row can raise
                self._spool.delete(rowid)
                self._note_unmappable(dest_id)
                continue
            self._pace()
            try:
                self._sender(dest.host, dest.port, dest.community, trap_oid, varbinds)
            except UnsendableTrap as exc:
                # The alarm's fault, not the destination's: drop it, keep the destination up, and
                # record it so an AppNeta retry is not spooled again.
                self._record(key_json, dest_id)
                self._spool.delete(rowid)
                self._unsendable.note(dest_id, str(exc))
                continue
            except Exception as exc:
                self._health.mark_down(dest_id, f"{type(exc).__name__}: {exc}")
                break
            self._record(key_json, dest_id)
            # drained counts delivery (the send succeeded), not the delete.
            with self._lock:
                self._drained[dest_id] = self._drained.get(dest_id, 0) + 1
            self._pending_delete[dest_id] = rowid
            self._spool.delete(rowid)
            del self._pending_delete[dest_id]
            sent += 1
        return sent

    def _record(self, key_json, dest_id) -> None:
        try:
            key = tuple(json.loads(key_json))
        except (ValueError, TypeError):  # an unreadable key must not keep a delivered row in the queue
            return
        if not (key and key[0] == UNDEDUPABLE):
            self._store.record((*key, dest_id))

    def _pace(self) -> None:
        """Start each send at least 1/rate after the previous start, sleeping only the remainder.

        The send time (including the refusal wait) counts toward the interval. The next slot is set
        from the actual start, so a slow or stalled send builds no credit and is never made up by a burst.
        """
        now = self._clock()
        if self._next_send is not None and now < self._next_send:
            self._sleep(self._next_send - now)
            now = self._clock()
        self._next_send = now + 1 / self._rate

    def _note_unmappable(self, dest_id) -> None:
        now = self._clock()
        with self._lock:
            self._unmappable[dest_id] = self._unmappable.get(dest_id, 0) + 1
            self._unmappable_since_log += 1
            last = self._last_unmappable_log
            if last is not None and now - last < UNMAPPABLE_LOG_INTERVAL_SECONDS:
                return
            count, self._unmappable_since_log = self._unmappable_since_log, 0
            self._last_unmappable_log = now
        logger.warning("event=dropped_unmappable dest=%s count=%d", dest_id, count)
