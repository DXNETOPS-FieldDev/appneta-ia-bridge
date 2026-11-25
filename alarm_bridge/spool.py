"""Per-destination alarm spool: a bounded SQLite queue in the state directory.

Unlike the dedup store, the spool never falls back to memory: any SQLite or OS error is raised as
``SpoolError`` so the caller can answer 503 and AppNeta retries. Size is bounded in layers: the
configured cap and the free-space floor drive eviction of the oldest rows (a raise together with its
clears), ``max_page_count`` is the hard backstop, and the WAL is checkpointed and truncated early.
"""

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from typing import Callable, NoReturn, Optional

logger = logging.getLogger("alarm_bridge")

DB_FILENAME = "spool.sqlite3"
CONTAINER_UID = 10001
PAGE_SIZE = 4096
EVICT_AT = 0.9  # evict once used pages pass this fraction of the effective cap
EVICTED_ALARM_MAX = 10_000
ERROR_LOG_INTERVAL_SECONDS = 60
WAL_MIN_BYTES = 64 * 1024
WAL_MAX_BYTES = 4 * 1024**2

_SCHEMA = (
    # (key_json, dest_id) is the identity; AUTOINCREMENT keeps a rowid from ever being reused, so a
    # drainer's delete(rowid) for a row eviction already removed cannot hit a newer row.
    "CREATE TABLE IF NOT EXISTS spool ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " key_json TEXT NOT NULL, dest_id TEXT NOT NULL, payload TEXT NOT NULL,"
    " alarm_id TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,"
    " UNIQUE (key_json, dest_id))",
    "CREATE INDEX IF NOT EXISTS spool_dest ON spool (dest_id)",
    "CREATE INDEX IF NOT EXISTS spool_alarm ON spool (alarm_id, dest_id)",
    "CREATE INDEX IF NOT EXISTS spool_state ON spool (state)",
    "CREATE TABLE IF NOT EXISTS evicted_alarm ("
    " dest_id TEXT NOT NULL, alarm_id TEXT NOT NULL, UNIQUE (dest_id, alarm_id))",
)


class SpoolError(Exception):
    """A spool read or write failed; the alarm is not durably stored."""


class ErrorLog:
    """``spool_error`` at most once per interval, with the count of failures the line covers."""

    def __init__(self, interval=ERROR_LOG_INTERVAL_SECONDS):
        self._interval = interval
        self._lock = threading.Lock()
        self._last = None
        self._pending = 0
        self._total = 0

    def total(self) -> int:
        """Failures reported since start, logged or not."""
        with self._lock:
            return self._total

    def report(self, exc, now):
        with self._lock:
            self._total += 1
            self._pending += 1
            if self._last is not None and now - self._last < self._interval:
                return
            count, self._pending, self._last = self._pending, 0, now
        logger.error("event=spool_error count=%d error=%s: %s", count, type(exc).__name__, exc)


_ERROR_LOG = ErrorLog()


def report_spool_error(exc, clock=time.time):
    """Log a spool failure through the shared rate limiter."""
    _ERROR_LOG.report(exc, clock())


def spool_error_count() -> int:
    """Spool failures reported since start (the ``spool_errors`` summary field)."""
    return _ERROR_LOG.total()


def check_state_dir(state_dir: str) -> None:
    """Raise SpoolError unless ``state_dir`` exists and the bridge can create files in it."""
    hint = f"make it a directory writable by uid {CONTAINER_UID} (the container user)"
    if not os.path.isdir(state_dir):
        raise SpoolError(f"state directory {state_dir} does not exist; {hint}")
    try:
        with tempfile.TemporaryFile(dir=state_dir):
            pass
    except OSError as exc:
        raise SpoolError(f"state directory {state_dir} is not writable ({exc.strerror}); {hint}") from exc


class SpoolStore:
    """Spooled webhooks keyed by ``(key_json, dest_id)``, oldest first per destination."""

    def __init__(self, state_dir: str, max_bytes: int, free_reserve_bytes: int, clock=time.time,
                 disk_usage=shutil.disk_usage, on_evict=None, timer: Callable = threading.Timer):
        self._state_dir = state_dir
        self._path = os.path.join(state_dir, DB_FILENAME)
        self._max_bytes = max_bytes
        self._reserve = free_reserve_bytes
        self._clock = clock
        self._disk_usage = disk_usage
        self._on_evict = on_evict
        self._timer = timer
        self._flush_timer = None  # one-shot spool_evicting flush for a rate-limited burst
        # The WAL counts toward the on-disk size, so its budget comes out of the cap.
        self._wal_limit = max(WAL_MIN_BYTES, min(WAL_MAX_BYTES, max_bytes // 16))
        self._lock = threading.Lock()
        self._evicted = {}
        self._evict_pending = {}
        self._last_evict_log = None
        self._conn = None
        try:
            try:
                self._conn = self._connect()
            except sqlite3.DatabaseError as exc:
                if isinstance(exc, sqlite3.OperationalError):
                    raise  # permissions, locking, I/O: not corruption, do not move the file
                self._quarantine(exc)
                self._conn = self._connect()
        except (sqlite3.Error, OSError) as exc:
            self._fail(exc)

    def _quarantine(self, exc) -> None:
        """Move a corrupt spool (and its WAL and shared-memory files) aside so the bridge can start."""
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self._clock()))
        aside = f"{self._path}.corrupt-{stamp}"
        n = 1
        while any(os.path.exists(aside + suffix) for suffix in ("", "-wal", "-shm")):
            n += 1  # a second quarantine in the same second must not overwrite the first
            aside = f"{self._path}.corrupt-{stamp}-{n}"
        for suffix in ("", "-wal", "-shm"):
            if suffix == "" or os.path.exists(self._path + suffix):
                os.replace(self._path + suffix, aside + suffix)
        logger.error(
            "event=spool_error error=%s: %s quarantined=%s (spool database corrupt: moved aside and an empty "
            "spool started; the alarms it held are not delivered; the file is kept for inspection and is "
            "not deleted automatically)",
            type(exc).__name__, exc, aside,
        )

    def _connect(self):
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(self._path, 0o600)
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise sqlite3.DatabaseError(f"quick_check: {check}")
            # page_size and auto_vacuum only take effect before the first table exists.
            conn.execute(f"PRAGMA page_size = {PAGE_SIZE}")
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            conn.execute("PRAGMA journal_mode = WAL").fetchone()
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute(f"PRAGMA journal_size_limit = {self._wal_limit}").fetchone()
            conn.execute(f"PRAGMA wal_autocheckpoint = {max(1, self._wal_limit // (2 * PAGE_SIZE))}")
            hard_pages = max(1, (self._max_bytes - self._wal_limit) // PAGE_SIZE)
            conn.execute(f"PRAGMA max_page_count = {hard_pages}").fetchone()
            for statement in _SCHEMA:
                conn.execute(statement)
            if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
                raise sqlite3.DatabaseError("spool database is not in incremental auto_vacuum mode")
        except BaseException:
            conn.close()
            raise
        return conn

    # -- public API -----------------------------------------------------------------------------

    def enqueue(self, key_json: str, dest_id: str, payload: dict, alarm_id: str, state: str) -> str:
        """Spool one webhook for one destination; ``"queued"`` or ``"duplicate"``."""
        body = json.dumps(payload, separators=(",", ":"))
        evicted, counts, reason, inserted = [], None, None, False
        with self._lock:
            try:
                conn = self._live()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    used_before = self._used_bytes(conn)
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO spool (key_json, dest_id, payload, alarm_id, state, created)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (key_json, dest_id, body, alarm_id, state, self._clock()),
                    )
                    inserted = cur.rowcount == 1
                    if inserted:
                        evicted, counts, reason = self._evict(conn, cur.lastrowid, used_before)
                    conn.execute("COMMIT")
                except BaseException:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                    raise
            except (sqlite3.Error, OSError) as exc:
                error = exc
            else:
                error = None
                if evicted:
                    self._note_eviction(evicted, counts, reason)
        if error is not None:
            self._fail(error)
        if evicted and self._on_evict is not None:
            self._on_evict(evicted)
        return "queued" if inserted else "duplicate"

    def depth(self, dest_id: str) -> int:
        return self._query("SELECT COUNT(*) FROM spool WHERE dest_id = ?", (dest_id,))[0]

    def has_backlog(self, dest_id: str) -> bool:
        """True when anything is queued for the destination. One index probe, unlike ``depth``'s count."""
        return self._query("SELECT 1 FROM spool WHERE dest_id = ? LIMIT 1", (dest_id,)) is not None

    def oldest_age(self, dest_id: str) -> float:
        created = self._query("SELECT MIN(created) FROM spool WHERE dest_id = ?", (dest_id,))[0]
        return 0.0 if created is None else max(0.0, self._clock() - created)

    def bytes_used(self) -> int:
        """Bytes in use by the database's pages, freelist excluded."""
        return self._read(lambda conn: self._used_bytes(conn))

    def oldest(self, dest_id: str) -> Optional[tuple[int, str, dict]]:
        row = self._query(
            "SELECT rowid, key_json, payload FROM spool WHERE dest_id = ? ORDER BY rowid LIMIT 1", (dest_id,)
        )
        if row is None:
            return None
        try:
            payload = json.loads(row[2])
        except ValueError:
            payload = {}  # unreadable: the drainer's mapping step drops and counts it, so it cannot stall the queue
        return row[0], row[1], payload

    def delete(self, rowid: int) -> None:
        self._read(lambda conn: conn.execute("DELETE FROM spool WHERE rowid = ?", (rowid,)))

    def raise_evicted(self, dest_id: str, alarm_id: str) -> bool:
        """True when a RAISED row for this alarm and destination was evicted."""
        row = self._query("SELECT 1 FROM evicted_alarm WHERE dest_id = ? AND alarm_id = ?", (dest_id, alarm_id))
        return row is not None

    def drop_orphans(self, configured_dest_ids: set[str]) -> int:
        """Delete rows for destinations that are no longer configured; return how many."""
        def drop(conn):
            present = {row[0] for row in conn.execute("SELECT DISTINCT dest_id FROM spool")}
            orphans = [(dest_id,) for dest_id in sorted(present - set(configured_dest_ids))]
            return conn.executemany("DELETE FROM spool WHERE dest_id = ?", orphans).rowcount if orphans else 0

        return self._read(drop)

    def counters(self, dest_id: str) -> dict:
        with self._lock:
            return {"evicted": self._evicted.get(dest_id, 0)}

    def close(self) -> None:
        with self._lock:
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None
            self._log_evictions()  # what a cancelled flush would have logged
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None

    # -- internals ------------------------------------------------------------------------------

    def _live(self):
        if self._conn is None:
            raise sqlite3.ProgrammingError("spool is closed")
        return self._conn

    def _read(self, fn):
        with self._lock:
            try:
                return fn(self._live())
            except (sqlite3.Error, OSError) as exc:
                error = exc
        self._fail(error)

    def _query(self, sql, params):
        return self._read(lambda conn: conn.execute(sql, params).fetchone())

    @staticmethod
    def _used_bytes(conn):
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        return (pages - free) * PAGE_SIZE

    def _threshold(self, used_before):
        """Return ``(used bytes to evict down to, reason)`` from the cap and the free-space floor."""
        # Configured cap: evict at 90% of it, leaving the WAL budget and some headroom.
        cap = max(0, self._max_bytes - self._wal_limit) * EVICT_AT
        # Free-space floor: do not grow past what is free beyond the reserve. No hysteresis here: it
        # would be taken from the spool's own size on every enqueue and shrink the spool to nothing.
        disk = used_before + self._disk_usage(self._state_dir).free - self._reserve
        return (cap, "cap") if cap <= disk else (max(0, disk), "disk")

    def _evict(self, conn, keep_rowid, used_before):
        """Delete oldest rows until under the threshold. Caller holds the lock and a transaction."""
        threshold, reason = self._threshold(used_before)
        evicted = []
        while self._used_bytes(conn) > threshold:
            group = self._next_group(conn, keep_rowid)
            if not group:
                break
            evicted.extend(group)
        if not evicted:
            return [], None, None
        conn.execute("PRAGMA incremental_vacuum").fetchall()
        return [(row[1], row[2]) for row in evicted], self._count_groups(evicted), reason

    def _next_group(self, conn, keep_rowid):
        """Delete and return the next eviction group as ``(rowid, key_json, dest_id, alarm_id, state)``."""
        raised = conn.execute(
            "SELECT rowid, key_json, dest_id, alarm_id, state FROM spool WHERE state = 'RAISED' AND rowid != ? ORDER BY rowid LIMIT 1",
            (keep_rowid,),
        ).fetchone()
        if raised is not None:
            _rowid, _key, dest_id, alarm_id, _state = raised
            group = [raised, *conn.execute(
                "SELECT rowid, key_json, dest_id, alarm_id, state FROM spool WHERE alarm_id = ? AND dest_id = ? AND state = 'CLEARED'"
                " AND rowid != ? ORDER BY rowid",
                (alarm_id, dest_id, keep_rowid),
            ).fetchall()]
            self._remember_evicted(conn, dest_id, alarm_id)
        else:
            lone = conn.execute(
                "SELECT rowid, key_json, dest_id, alarm_id, state FROM spool WHERE rowid != ? ORDER BY rowid LIMIT 1", (keep_rowid,)
            ).fetchone()
            if lone is None:
                return []
            group = [lone]
        conn.executemany("DELETE FROM spool WHERE rowid = ?", [(row[0],) for row in group])
        return group

    @staticmethod
    def _remember_evicted(conn, dest_id, alarm_id):
        conn.execute("INSERT OR IGNORE INTO evicted_alarm (dest_id, alarm_id) VALUES (?, ?)", (dest_id, alarm_id))
        excess = conn.execute("SELECT COUNT(*) FROM evicted_alarm").fetchone()[0] - EVICTED_ALARM_MAX
        if excess > 0:
            conn.execute(
                "DELETE FROM evicted_alarm WHERE rowid IN (SELECT rowid FROM evicted_alarm ORDER BY rowid LIMIT ?)",
                (excess,),
            )

    @staticmethod
    def _count_groups(evicted):
        counts = {"pairs": 0, "lone_raises": 0, "lone_clears": 0}
        raised = {(row[2], row[3]) for row in evicted if row[4] == "RAISED"}
        cleared = {(row[2], row[3]) for row in evicted if row[4] == "CLEARED"}
        counts["pairs"] = len(raised & cleared)
        counts["lone_raises"] = len(raised - cleared)
        counts["lone_clears"] = sum(1 for row in evicted if row[4] == "CLEARED" and (row[2], row[3]) not in raised)
        return counts

    def _note_eviction(self, evicted, counts, reason):
        # Caller holds self._lock. At the cap every enqueue evicts, so log at most once a minute.
        for _key, dest_id in evicted:
            self._evicted[dest_id] = self._evicted.get(dest_id, 0) + 1
        pending = self._evict_pending.setdefault(reason, {"count": 0, "pairs": 0, "lone_raises": 0, "lone_clears": 0})
        pending["count"] += len(evicted)
        for name, value in counts.items():
            pending[name] += value
        now = self._clock()
        if self._last_evict_log is not None and now - self._last_evict_log < ERROR_LOG_INTERVAL_SECONDS:
            if self._flush_timer is None:
                # Log the held-back counts when the window ends, even if no further eviction comes.
                # Clamped: the clock is wall time, and a step back must not push the flush out.
                delay = min(ERROR_LOG_INTERVAL_SECONDS, self._last_evict_log + ERROR_LOG_INTERVAL_SECONDS - now)
                self._flush_timer = self._timer(delay, self._flush_evictions)
                self._flush_timer.daemon = True
                self._flush_timer.start()
            return
        self._log_evictions()

    def _flush_evictions(self):
        with self._lock:
            self._flush_timer = None
            self._log_evictions()

    def _log_evictions(self):
        # Caller holds self._lock.
        if not self._evict_pending:
            return
        self._last_evict_log = self._clock()
        for why, c in sorted(self._evict_pending.items()):
            logger.warning(
                "event=spool_evicting reason=%s count=%d pairs=%d lone_raises=%d lone_clears=%d "
                "(oldest spooled alarms dropped)",
                why, c["count"], c["pairs"], c["lone_raises"], c["lone_clears"],
            )
        self._evict_pending = {}

    def _fail(self, exc) -> NoReturn:
        report_spool_error(exc, self._clock)
        raise SpoolError(f"{type(exc).__name__}: {exc}") from exc
