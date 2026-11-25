"""Webhook de-duplication: the dedup key, a striped keyed lock and the seen-key store.

The store fails open: a SQLite or OS error makes that call return False (``seen``) or do nothing
(``record``), then drops to the in-memory store (mode ``memory-degraded``), so a store problem can
cause a duplicate trap but never a dropped alarm.
"""

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict

from alarm_bridge.mapping import AlarmMappingError, _resolve_state_and_timestamp

logger = logging.getLogger("alarm_bridge")

DB_FILENAME = "dedup.sqlite3"
MAX_KEY_PART_LEN = 256
PURGE_INTERVAL_SECONDS = 600
ERROR_LOG_INTERVAL_SECONDS = 60


def dedup_key(payload):
    """Return ``(alarmId, state, timestamp)`` or None when the payload is not dedupable.

    State and timestamp follow the mapper's rule, so the key and the trap agree. Never raises.
    """
    try:
        state, timestamp = _resolve_state_and_timestamp(payload)
    except AlarmMappingError:
        return None
    key = (payload.get("alarmId"), state, timestamp)
    for part in key:
        if not isinstance(part, str) or not 0 < len(part) <= MAX_KEY_PART_LEN:
            return None
    return key


class KeyedLocks:
    """One lock per key in flight, counted and dropped when unused.

    Exact keys, not hashed stripes: a copy stuck in a slow send delays only a re-POST of the same
    alarm, never an unrelated one. Memory is bounded by the requests in flight.
    """

    def __init__(self):
        self._guard = threading.Lock()
        self._entries = {}  # key -> [lock, requests holding or waiting]

    def acquire(self, key, timeout):
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = self._entries[key] = [threading.Lock(), 0]
            entry[1] += 1
        if entry[0].acquire(timeout=timeout):
            return True
        self._drop(key)
        return False

    def release(self, key):
        with self._guard:
            entry = self._entries[key]
        entry[0].release()
        self._drop(key)

    def _drop(self, key):
        with self._guard:
            entry = self._entries[key]
            entry[1] -= 1
            if entry[1] == 0:
                del self._entries[key]


class DedupStore:
    """Seen-key store with TTL and a size cap. Empty ``state_dir`` means in-memory only."""

    def __init__(self, state_dir, ttl_hours, max_entries, clock=time.time, on_error=None):
        self._ttl = ttl_hours * 3600
        self._max_entries = max_entries
        self._clock = clock
        self._on_error = on_error
        self._lock = threading.Lock()
        self._err_lock = threading.Lock()
        self._last_error_log = None
        self._last_evict_log = None
        self._evicted_since_log = 0
        self._memory = OrderedDict()
        self._conn = None
        self._rows = 0  # rows in ``seen`` while SQLite is live; kept by record and purge, never re-counted
        self._closed = False
        self._db_path = os.path.join(state_dir, DB_FILENAME) if state_dir else None
        if not state_dir:
            self._mode = "memory"
        else:
            try:
                conn = self._open_sqlite(self._db_path)
                try:
                    rows = self._count_rows(conn)
                except BaseException:
                    conn.close()
                    raise
                self._conn, self._rows, self._mode = conn, rows, "sqlite"
            except (sqlite3.Error, OSError) as exc:
                self._mode = "memory-degraded"
                logger.error(
                    "event=dedup_store_degraded state_dir=%s error=%s: %s "
                    "(dedup state is in memory only; make the directory writable by the container user)",
                    state_dir, type(exc).__name__, exc,
                )
        self._stop = threading.Event()
        self._purge_thread = threading.Thread(target=self._purge_loop, name="dedup-purge", daemon=True)
        self._purge_thread.start()

    @property
    def mode(self):
        return self._mode

    # -- SQLite setup ---------------------------------------------------------------------------

    def _open_sqlite(self, path):
        try:
            return self._connect(path)
        except sqlite3.DatabaseError as exc:
            if isinstance(exc, sqlite3.OperationalError):
                raise  # permissions, locking, I/O: not corruption, do not move the file
            aside = f"{path}.corrupt-{int(self._clock())}"
            os.replace(path, aside)
            if os.path.exists(path + "-journal"):
                os.replace(path + "-journal", aside + "-journal")
            logger.error(
                "event=dedup_store_corrupt path=%s moved_to=%s error=%s: %s",
                path, aside, type(exc).__name__, exc,
            )
            return self._connect(path)

    def _connect(self, path):
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise sqlite3.DatabaseError(f"quick_check: {check}")
            conn.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts REAL NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS seen_ts ON seen (ts)")
            conn.execute("DELETE FROM seen WHERE ts <= ?", (self._clock() - self._ttl,))
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _count_rows(conn):
        return conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    @staticmethod
    def _db_key(key):
        # JSON, not a plain separator join: a separator inside a field could make two keys collide.
        return json.dumps(list(key), separators=(",", ":"))

    # -- public API -----------------------------------------------------------------------------

    def seen(self, key):
        now = self._clock()
        with self._lock:
            if self._conn is None:
                ts = self._memory.get(key)
                if ts is not None and ts <= now - self._ttl:
                    del self._memory[key]
                    ts = None
                return ts is not None
            conn = self._conn
            try:
                row = conn.execute("SELECT ts FROM seen WHERE key = ?", (self._db_key(key),)).fetchone()
                return row is not None and row[0] > now - self._ttl
            except (sqlite3.Error, OSError) as exc:
                error = exc
        self._fail(error, conn)
        return False

    def record(self, key):
        now = self._clock()
        with self._lock:
            if self._conn is None:
                self._memory[key] = now
                self._memory.move_to_end(key)
                evicted = 0
                while len(self._memory) > self._max_entries:
                    self._memory.popitem(last=False)
                    evicted += 1
                self._log_eviction(evicted, now)
                return
            conn = self._conn
            try:
                self._record_sqlite(conn, key, now)
                return
            except (sqlite3.Error, OSError) as exc:
                error = exc
        self._fail(error, conn)

    def _record_sqlite(self, conn, key, now):
        """Insert or refresh ``key`` and evict down to the cap in one transaction (one fsync).

        Caller holds the lock. ``_rows`` is only trusted after the commit: a failure drops the
        connection (``_fail``), and a later recovery counts the table afresh.
        """
        db_key = self._db_key(key)
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._rows
            if conn.execute("INSERT OR IGNORE INTO seen (key, ts) VALUES (?, ?)", (db_key, now)).rowcount:
                rows += 1
            else:
                conn.execute("UPDATE seen SET ts = ? WHERE key = ?", (now, db_key))
            evicted = 0
            if rows > self._max_entries:
                evicted = conn.execute(
                    "DELETE FROM seen WHERE key IN (SELECT key FROM seen ORDER BY ts LIMIT ?)",
                    (rows - self._max_entries,),
                ).rowcount
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        self._rows = rows - evicted
        self._log_eviction(evicted, now)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._purge_thread.join(5)
        with self._lock:
            self._log_eviction(0, self._clock(), force=True)  # evictions still waiting for their log window
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- internals ------------------------------------------------------------------------------

    def _log_eviction(self, evicted, now, force=False):
        # Caller holds self._lock. At the cap every new key evicts one, so limit to one line a minute.
        self._evicted_since_log += evicted
        if not self._evicted_since_log:
            return
        if not force and self._last_evict_log is not None and now - self._last_evict_log < ERROR_LOG_INTERVAL_SECONDS:
            return
        logger.warning(
            "event=dedup_evict evicted=%d max_entries=%d (oldest entries dropped)",
            self._evicted_since_log, self._max_entries,
        )
        self._last_evict_log = now
        self._evicted_since_log = 0

    def _purge(self):
        cutoff = self._clock() - self._ttl
        with self._lock:
            if self._conn is None:
                for key in [k for k, ts in self._memory.items() if ts <= cutoff]:
                    del self._memory[key]
                degraded = self._mode == "memory-degraded" and not self._closed
            else:
                degraded = False
        if degraded:
            self._recover()
            return
        with self._lock:
            if self._conn is None:
                return
            conn = self._conn
            try:
                conn.execute("DELETE FROM seen WHERE ts <= ?", (cutoff,))
                self._rows = self._count_rows(conn)  # re-sync: keeps any drift from outliving a purge tick
                return
            except (sqlite3.Error, OSError) as exc:
                error = exc
        self._fail(error, conn)

    def _recover(self):
        """Reopen SQLite after a runtime failure and carry the in-memory keys back into it.

        Runs from the purge tick, so a fixed disk or directory is picked up within
        ``PURGE_INTERVAL_SECONDS`` without a restart. A failed attempt is silent: the failure that
        degraded the store was already logged, and one line per tick would only repeat it.
        """
        try:
            conn = self._open_sqlite(self._db_path)
        except (sqlite3.Error, OSError):
            return
        with self._lock:
            if self._closed or self._conn is not None:
                conn.close()
                return
            now = self._clock()
            carried = [(self._db_key(k), ts) for k, ts in self._memory.items() if ts > now - self._ttl]
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    "INSERT INTO seen (key, ts) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET ts = max(ts, excluded.ts)",
                    carried,
                )
                rows = self._count_rows(conn)
                evicted = 0
                if rows > self._max_entries:
                    evicted = conn.execute(
                        "DELETE FROM seen WHERE key IN (SELECT key FROM seen ORDER BY ts LIMIT ?)",
                        (rows - self._max_entries,),
                    ).rowcount
                conn.execute("COMMIT")
            except (sqlite3.Error, OSError):
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
                return
            self._conn, self._rows, self._mode = conn, rows - evicted, "sqlite"
            self._memory.clear()
            self._log_eviction(evicted, now)
        logger.warning("event=dedup_store_recovered carried=%d (dedup state is back in SQLite)", len(carried))

    def _purge_loop(self):
        while not self._stop.wait(PURGE_INTERVAL_SECONDS):
            try:
                self._purge()
            except Exception as exc:  # a dead purge thread would silently end both purging and recovery
                logger.error("event=dedup_purge_error error=%s: %s", type(exc).__name__, exc)

    def _fail(self, exc, failed=None):
        """Drop to memory after ``exc``. ``failed`` is the connection that raised: if a recovery has
        replaced it since, that connection is closed but the new one is left alone."""
        with self._lock:
            stale = failed is not None and self._conn is not failed
            conn = failed if stale else self._conn
            if not stale:
                self._conn = None
                if conn is not None:
                    self._mode = "memory-degraded"
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):  # already unusable
                conn.close()
        if self._on_error is not None:
            self._on_error(exc)
        now = self._clock()
        with self._err_lock:
            if self._last_error_log is not None and now - self._last_error_log < ERROR_LOG_INTERVAL_SECONDS:
                return
            self._last_error_log = now
        logger.error(
            "event=dedup_store_error mode=%s error=%s: %s (processing without dedup)",
            self._mode, type(exc).__name__, exc,
        )
