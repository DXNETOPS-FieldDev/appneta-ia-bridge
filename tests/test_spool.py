import json
import os
import sqlite3
import tempfile
import unittest
from collections import namedtuple
from unittest import mock

from alarm_bridge import spool
from alarm_bridge.spool import DB_FILENAME, SpoolError, SpoolStore, check_state_dir

Usage = namedtuple("Usage", "total used free")
BIG = 40_000  # a payload this size lives in overflow pages, which a delete frees exactly
LOTS = 10 * 1024**3


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeDisk:
    def __init__(self, free=LOTS):
        self.free = free

    def __call__(self, path):
        return Usage(LOTS, 0, self.free)


def key(alarm_id, state="RAISED"):
    return json.dumps([alarm_id, state, "t"])


def payload(alarm_id, state="RAISED", size=100):
    return {"alarmId": alarm_id, "state": state, "pad": "x" * size}


class BadCheck:
    """A connection whose quick_check reports damage; everything else goes to the real one."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        if sql == "PRAGMA quick_check":
            return self._conn.execute("SELECT '*** in database main *** Page 2: btree damaged'")
        return self._conn.execute(sql, *args)

    def close(self):
        self._conn.close()


class SpoolTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.clock = FakeClock()
        self.disk = FakeDisk()
        self.evicted = []
        self._stores = []

    def tearDown(self):
        for store in self._stores:
            store.close()
        self._tmp.cleanup()

    def open(self, max_bytes=64 * 1024**2, reserve=0):
        store = SpoolStore(self.dir, max_bytes, reserve, clock=self.clock, disk_usage=self.disk,
                           on_evict=self.evicted.append)
        self._stores.append(store)
        return store

    def head(self, store, dest="slot1"):
        row = store.oldest(dest)
        assert row is not None, f"spool for {dest} is empty"
        return row

    def put(self, store, alarm_id, state="RAISED", dest="slot1", size=100):
        return store.enqueue(key(alarm_id, state), dest, payload(alarm_id, state, size), alarm_id, state)

    def fill_until_evicting(self, store, state="RAISED", dest="slot1", limit=5000):
        """Add small filler rows until the first eviction; return the first evicted batch."""
        for i in range(limit):
            self.put(store, f"fill-{i}", state, dest, size=2000)
            if self.evicted:
                return self.evicted[0]
        self.fail("no eviction happened")


class TestBasics(SpoolTestCase):
    def test_enqueue_then_oldest_roundtrip(self):
        store = self.open()
        self.assertEqual(self.put(store, "a1"), "queued")
        rowid, key_json, body = self.head(store)
        self.assertIsInstance(rowid, int)
        self.assertEqual(key_json, key("a1"))
        self.assertEqual(body, payload("a1"))
        self.assertEqual(store.depth("slot1"), 1)
        self.assertIsNone(store.oldest("slot2"))

    def test_use_after_close_raises_spool_error_and_does_not_reopen(self):
        # A drainer wedged past shutdown must get an error, not a fresh connection to a closed spool.
        store = self.open()
        self.put(store, "a1")
        store.close()
        with self.assertRaises(SpoolError):
            store.oldest("slot1")
        with self.assertRaises(SpoolError):
            self.put(store, "a2")

    def test_has_backlog_is_per_destination_and_clears_on_delete(self):
        store = self.open()
        self.assertFalse(store.has_backlog("slot1"))
        self.put(store, "a1")
        self.assertTrue(store.has_backlog("slot1"))
        self.assertFalse(store.has_backlog("slot2"))
        store.delete(self.head(store)[0])
        self.assertFalse(store.has_backlog("slot1"))

    def test_duplicate_key_dest_ignored(self):
        store = self.open()
        self.assertEqual(self.put(store, "a1"), "queued")
        self.assertEqual(self.put(store, "a1"), "duplicate")
        self.assertEqual(store.depth("slot1"), 1)

    def test_same_key_two_destinations_two_rows(self):
        store = self.open()
        self.assertEqual(self.put(store, "a1", dest="slot1"), "queued")
        self.assertEqual(self.put(store, "a1", dest="slot2"), "queued")
        self.assertEqual(store.depth("slot1"), 1)
        self.assertEqual(store.depth("slot2"), 1)

    def test_oldest_first_and_delete(self):
        store = self.open()
        self.put(store, "a1")
        self.clock.advance(5)
        self.put(store, "a2")
        self.clock.advance(5)
        self.assertEqual(store.oldest_age("slot1"), 10)
        self.assertEqual(store.oldest_age("slot2"), 0)
        rowid, key_json, _ = self.head(store)
        self.assertEqual(key_json, key("a1"))
        store.delete(rowid)
        self.assertEqual(self.head(store)[1], key("a2"))
        self.assertEqual(store.oldest_age("slot1"), 5)
        store.delete(self.head(store)[0])
        self.assertIsNone(store.oldest("slot1"))
        self.assertEqual(store.depth("slot1"), 0)

    def test_rowid_never_reused(self):
        # A drainer holds a rowid while sending; if eviction removed that row, a new row must not
        # take the same rowid, or the drainer's delete() would drop an alarm that was never sent.
        store = self.open()
        self.put(store, "a1")
        stale = self.head(store)[0]
        store.delete(stale)
        self.put(store, "a2")
        self.assertNotEqual(self.head(store)[0], stale)

    def test_drop_orphans(self):
        store = self.open()
        self.put(store, "a1", dest="slot1")
        self.put(store, "a2", dest="slot2")
        self.put(store, "a3", dest="slot3")
        self.assertEqual(store.drop_orphans({"slot1", "slot3"}), 1)
        self.assertEqual(store.depth("slot2"), 0)
        self.assertEqual(store.depth("slot1"), 1)
        self.assertEqual(store.depth("slot3"), 1)

    def test_persists_across_reopen(self):
        store = self.open()
        self.put(store, "a1")
        store.close()
        again = self.open()
        self.assertEqual(again.depth("slot1"), 1)
        self.assertEqual(self.head(again)[2], payload("a1"))
        self.assertEqual(self.put(again, "a1"), "duplicate")

    def test_file_is_private_and_incremental_vacuum(self):
        store = self.open()
        path = os.path.join(self.dir, DB_FILENAME)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("PRAGMA auto_vacuum").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()
        store.close()


class TestEviction(SpoolTestCase):
    def test_eviction_at_cap_drops_oldest_keeps_newest(self):
        store = self.open(max_bytes=400 * 1024)
        with self.assertLogs("alarm_bridge", level="WARNING") as logs:
            for i in range(400):
                self.put(store, f"a{i}", size=2000)
        self.assertTrue(self.evicted)
        evicted_keys = [k for batch in self.evicted for k, _ in batch]
        self.assertEqual(evicted_keys, [key(f"a{i}") for i in range(len(evicted_keys))])  # oldest first
        self.assertEqual(self.head(store)[1], key(f"a{len(evicted_keys)}"))
        self.assertEqual(store.depth("slot1") + len(evicted_keys), 400)
        self.assertEqual(self.put(store, "a399"), "duplicate")
        self.assertEqual(store.counters("slot1")["evicted"], len(evicted_keys))
        self.assertTrue(any("event=spool_evicting" in m and "reason=cap" in m for m in logs.output))
        self.assertLessEqual(store.bytes_used(), 400 * 1024)

    def test_free_space_floor_evicts_with_reason_disk(self):
        store = self.open(reserve=1024**2)
        for i in range(20):
            self.put(store, f"a{i}", size=2000)
        self.assertEqual(self.evicted, [])
        self.disk.free = 1024**2 - 20_000  # below the reserve: the spool must give space back
        with self.assertLogs("alarm_bridge", level="WARNING") as logs:
            self.put(store, "new", size=2000)
        self.assertTrue(self.evicted)
        self.assertTrue(any("event=spool_evicting" in m and "reason=disk" in m for m in logs.output))
        self.assertEqual(self.evicted[0][0], (key("a0"), "slot1"))  # oldest first
        self.assertGreater(store.depth("slot1"), 5)  # gives back the deficit, not the whole spool

    def test_holds_size_at_free_space_floor(self):
        # At the floor the spool must stop growing, not shrink: each new alarm costs about one old one.
        store = self.open(max_bytes=256 * 1024**2, reserve=128 * 1024**2)
        for i in range(2000):
            self.put(store, f"a{i}", size=1400)
        self.assertEqual(self.evicted, [])
        self.disk.free = 128 * 1024**2  # pinned at the reserve
        for i in range(200):
            self.put(store, f"new{i}", size=1400)
        self.assertTrue(self.evicted)
        self.assertGreaterEqual(store.depth("slot1"), 2000 - 20)

    def test_never_exceeds_hard_cap_on_disk(self):
        cap = 1024**2
        store = self.open(max_bytes=cap)
        path = os.path.join(self.dir, DB_FILENAME)

        def on_disk():
            # Everything SQLite keeps for this database: main file, WAL, shared-memory index.
            return sum(os.path.getsize(p) for p in (path, path + "-wal", path + "-shm") if os.path.exists(p))

        peak = 0
        for i in range(5000):
            store.enqueue(key(f"a{i}"), "slot1", payload(f"a{i}", size=1500), f"a{i}", "RAISED")
            if i % 50 == 0:
                peak = max(peak, on_disk())
        peak = max(peak, on_disk())
        self.assertLessEqual(peak, cap + 64 * 1024)
        self.assertGreater(store.depth("slot1"), 100)  # evicting, not emptying
        self.assertEqual(self.put(store, "a4999", size=1500), "duplicate")

    def test_evicting_a_raise_also_drops_its_clear(self):
        store = self.open(max_bytes=600 * 1024)
        self.put(store, "A", "RAISED", size=BIG)
        self.put(store, "B", "RAISED")
        self.put(store, "A", "CLEARED", size=BIG)
        batch = self.fill_until_evicting(store)
        self.assertEqual(batch, [(key("A", "RAISED"), "slot1"), (key("A", "CLEARED"), "slot1")])
        self.assertEqual(self.head(store)[1], key("B"))

    def test_evicting_a_clear_drops_nothing_else(self):
        store = self.open(max_bytes=600 * 1024)
        self.put(store, "X", "CLEARED", size=BIG)
        self.put(store, "Y", "CLEARED")
        batch = self.fill_until_evicting(store, state="CLEARED")
        self.assertEqual(batch, [(key("X", "CLEARED"), "slot1")])
        self.assertEqual(self.head(store)[1], key("Y", "CLEARED"))
        self.assertFalse(store.raise_evicted("slot1", "X"))

    def test_lone_clear_is_evicted_last(self):
        store = self.open(max_bytes=600 * 1024)
        self.put(store, "X", "CLEARED", size=BIG)
        self.put(store, "A", "RAISED", size=BIG)
        self.put(store, "B", "RAISED", size=BIG)
        batch = self.fill_until_evicting(store)
        self.assertEqual(batch, [(key("A"), "slot1")])
        self.assertEqual(self.head(store)[1], key("X", "CLEARED"))

    def test_lone_clear_evicted_when_nothing_else_evictable(self):
        store = self.open(max_bytes=600 * 1024)
        self.put(store, "X", "CLEARED", size=BIG)
        self.put(store, "Y", "CLEARED", size=BIG)
        batch = self.fill_until_evicting(store, state="CLEARED")
        self.assertEqual(batch, [(key("X", "CLEARED"), "slot1")])

    def test_raise_on_other_destination_is_not_a_pair(self):
        store = self.open(max_bytes=600 * 1024)
        self.put(store, "A", "RAISED", dest="slot1", size=BIG)
        self.put(store, "A", "CLEARED", dest="slot2", size=BIG)
        batch = self.fill_until_evicting(store)
        self.assertEqual(batch, [(key("A"), "slot1")])
        self.assertEqual(store.depth("slot2"), 1)

    def test_clear_arriving_after_its_raise_was_evicted_is_reported_by_raise_evicted(self):
        store = self.open(max_bytes=600 * 1024)
        self.put(store, "A", "RAISED", size=BIG)
        self.fill_until_evicting(store)
        self.assertTrue(store.raise_evicted("slot1", "A"))
        self.assertFalse(store.raise_evicted("slot2", "A"))
        self.assertFalse(store.raise_evicted("slot1", "fill-0"))
        store.close()
        self.assertTrue(self.open(max_bytes=600 * 1024).raise_evicted("slot1", "A"))

    def test_evicted_alarm_table_is_bounded_to_10000(self):
        self.assertEqual(spool.EVICTED_ALARM_MAX, 10_000)
        store = self.open(max_bytes=600 * 1024)
        conn = store._conn
        assert conn is not None
        conn.executemany("INSERT INTO evicted_alarm (dest_id, alarm_id) VALUES ('slot1', ?)",
                         [(f"old-{i}",) for i in range(10_000)])
        self.put(store, "A", "RAISED", size=BIG)
        self.fill_until_evicting(store)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM evicted_alarm").fetchone()[0], 10_000)
        self.assertTrue(store.raise_evicted("slot1", "A"))
        self.assertFalse(store.raise_evicted("slot1", "old-0"))
        self.assertTrue(store.raise_evicted("slot1", "old-9999"))

    def test_spool_evicting_log_reports_pairs_and_lone_counts(self):
        store = self.open(reserve=10**9)  # free (LOTS) covers it until free drops to 0 below
        self.put(store, "A", "RAISED", size=BIG)
        self.put(store, "A", "CLEARED", size=BIG)
        self.put(store, "B", "RAISED", size=BIG)
        self.put(store, "C", "CLEARED", size=BIG)
        self.disk.free = 0
        with self.assertLogs("alarm_bridge", level="WARNING") as logs:
            self.put(store, "new", "RAISED")
        line = next(m for m in logs.output if "event=spool_evicting" in m)
        for part in ("reason=disk", "count=4", "pairs=1", "lone_raises=1", "lone_clears=1"):
            self.assertIn(part, line)
        self.assertEqual(store.depth("slot1"), 1)
        self.assertEqual(self.head(store)[1], key("new"))

    def test_undedupable_rows_are_not_counted_as_lone_clears(self):
        # A row with no state (an undedupable alarm) is neither a raise nor a clear.
        rows = [(1, "k1", "slot1", "A", "RAISED"), (2, "k2", "slot1", "A", "CLEARED"),
                (3, "k3", "slot1", "B", ""), (4, "k4", "slot1", "C", "CLEARED")]
        self.assertEqual(SpoolStore._count_groups(rows), {"pairs": 1, "lone_raises": 0, "lone_clears": 1})


class FakeTimer:
    def __init__(self, interval, fn):
        self.interval, self.fn = interval, fn
        self.daemon = self.started = self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class TestEvictionLogFlush(SpoolTestCase):
    """A rate-limited burst's remainder is logged at the end of the window, not at the next eviction."""

    def setUp(self):
        super().setUp()
        self.timers = []

    def open_timed(self):
        def timer(interval, fn):
            self.timers.append(FakeTimer(interval, fn))
            return self.timers[-1]

        store = SpoolStore(self.dir, 400 * 1024, 0, clock=self.clock, disk_usage=self.disk,
                           on_evict=self.evicted.append, timer=timer)
        self._stores.append(store)
        return store

    @staticmethod
    def counts(logs):
        return [int(m.split("count=")[1].split()[0]) for m in logs.output if "event=spool_evicting" in m]

    def flood(self, store, batches, prefix="a"):
        """Enqueue until ``batches`` more evictions happened."""
        target = len(self.evicted) + batches
        for i in range(5000):
            self.put(store, f"{prefix}{i}", size=2000)
            if len(self.evicted) >= target:
                return
        self.fail("not enough evictions")

    def test_burst_remainder_logged_at_end_of_window(self):
        store = self.open_timed()
        with self.assertLogs("alarm_bridge", level="WARNING") as logs:
            self.flood(store, 5)
        self.assertEqual(len(self.counts(logs)), 1)  # the first line goes out at once
        self.assertEqual(len(self.timers), 1)  # one flush armed for the rest, not one per eviction
        timer = self.timers[0]
        self.assertTrue(timer.started and timer.daemon)
        self.assertEqual(timer.interval, 60)
        self.clock.advance(60)
        with self.assertLogs("alarm_bridge", level="WARNING") as later:
            timer.fn()
        total = store.counters("slot1")["evicted"]
        self.assertEqual(self.counts(logs) + self.counts(later), [self.counts(logs)[0], total - self.counts(logs)[0]])

    def test_flush_rearms_only_when_more_pending(self):
        store = self.open_timed()
        with self.assertLogs("alarm_bridge", level="WARNING"):
            self.flood(store, 3)
        self.clock.advance(60)
        with self.assertLogs("alarm_bridge", level="WARNING"):
            self.timers[0].fn()
        self.assertEqual(len(self.timers), 1)  # nothing pending: nothing re-armed
        self.clock.advance(10)
        self.flood(store, 1, prefix="b")  # inside the new window: held back, flush armed again
        self.assertEqual(len(self.timers), 2)
        self.assertEqual(self.timers[1].interval, 50)
        self.clock.advance(50)
        with self.assertLogs("alarm_bridge", level="WARNING") as logs:
            self.timers[1].fn()
        self.assertEqual(len(self.counts(logs)), 1)

    def test_flush_with_nothing_pending_logs_nothing(self):
        store = self.open_timed()
        with self.assertLogs("alarm_bridge", level="WARNING"):
            self.flood(store, 2)
        self.clock.advance(60)
        with self.assertLogs("alarm_bridge", level="WARNING"):
            self.timers[0].fn()
        with self.assertNoLogs("alarm_bridge", level="WARNING"):
            self.timers[0].fn()

    def test_close_cancels_flush_and_logs_remainder(self):
        store = self.open_timed()
        with self.assertLogs("alarm_bridge", level="WARNING") as logs:
            self.flood(store, 3)
        first = self.counts(logs)[0]
        with self.assertLogs("alarm_bridge", level="WARNING") as closing:
            store.close()
        self.assertTrue(self.timers[0].cancelled)
        self.assertEqual(self.counts(closing), [store.counters("slot1")["evicted"] - first])


class TestErrors(SpoolTestCase):
    def test_unwritable_dir_raises_spoolerror_with_path_and_uid(self):
        missing = os.path.join(self.dir, "nope")
        with self.assertRaises(SpoolError) as ctx:
            check_state_dir(missing)
        self.assertIn(missing, str(ctx.exception))
        self.assertIn("10001", str(ctx.exception))
        if os.geteuid() == 0:
            self.skipTest("root can write a read-only directory")
        locked = os.path.join(self.dir, "locked")
        os.mkdir(locked, 0o500)
        try:
            with self.assertRaises(SpoolError) as ctx:
                check_state_dir(locked)
            self.assertIn(locked, str(ctx.exception))
            self.assertIn("10001", str(ctx.exception))
        finally:
            os.chmod(locked, 0o700)
        check_state_dir(self.dir)  # a writable directory passes

    def _garbage(self):
        path = os.path.join(self.dir, DB_FILENAME)
        with open(path, "wb") as fh:
            fh.write(b"this is not a sqlite database" * 200)
        return path

    def test_corrupt_spool_is_quarantined_and_a_fresh_one_used(self):
        # A stale -wal next to a garbage file is removed by SQLite itself when the failed connection
        # closes (a WAL-mode file that fails quick_check is checkpointed into the main file first), so
        # only the main file is checked here; any -wal/-shm still present is moved along with it.
        path = self._garbage()
        self.clock.now = 1_795_000_000.0  # 2026-11-18T11:06:40Z
        with self.assertLogs("alarm_bridge", level="ERROR") as logs:
            store = self.open()
        aside = path + ".corrupt-20261118T110640Z"
        with open(aside, "rb") as fh:
            self.assertTrue(fh.read().startswith(b"this is not a sqlite database"))
        self.assertNotIn(":", os.path.basename(aside))
        line = next(m for m in logs.output if "event=spool_error" in m)
        self.assertIn(aside, line)
        self.assertEqual(self.put(store, "a1"), "queued")  # usable
        self.assertEqual(store.depth("slot1"), 1)
        self.assertLess(store.bytes_used(), 100 * 1024)  # the quarantined file is not counted

    def test_second_quarantine_in_the_same_second_keeps_the_first_file(self):
        path = self._garbage()
        self.clock.now = 1_795_000_000.0
        with self.assertLogs("alarm_bridge", level="ERROR"):
            self.open().close()
        self._garbage()
        with open(path, "wb") as fh:
            fh.write(b"second corrupt file, not sqlite either, padded out to look like a header....")
        with self.assertLogs("alarm_bridge", level="ERROR"):
            self.open().close()
        first = path + ".corrupt-20261118T110640Z"
        second = path + ".corrupt-20261118T110640Z-2"
        with open(first, "rb") as fh:
            self.assertTrue(fh.read().startswith(b"this is not a sqlite database"))
        with open(second, "rb") as fh:
            self.assertTrue(fh.read().startswith(b"second corrupt file"))

    def test_quarantine_moves_wal_and_shm_along(self):
        path = self._garbage()
        for suffix in ("-wal", "-shm"):
            with open(path + suffix, "wb") as fh:
                fh.write(b"sibling")
        real = SpoolStore._connect
        calls = []

        def connect(store):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.DatabaseError("file is not a database")  # before SQLite could touch the WAL
            return real(store)

        self.clock.now = 1_795_000_000.0
        with mock.patch.object(SpoolStore, "_connect", connect), self.assertLogs("alarm_bridge", level="ERROR"):
            self.open()
        aside = path + ".corrupt-20261118T110640Z"
        for suffix in ("", "-wal", "-shm"):
            self.assertTrue(os.path.exists(aside + suffix), suffix)

    def test_failed_quick_check_is_quarantined(self):
        self.open()  # a real spool file
        self._stores.pop().close()
        real_connect = sqlite3.connect
        calls = []

        def fake_connect(*args, **kwargs):
            calls.append(1)
            conn = real_connect(*args, **kwargs)
            return BadCheck(conn) if len(calls) == 1 else conn

        with mock.patch.object(spool.sqlite3, "connect", side_effect=fake_connect), \
                self.assertLogs("alarm_bridge", level="ERROR"):
            store = self.open()
        self.assertEqual(len([n for n in os.listdir(self.dir) if ".corrupt-" in n and not n.endswith(("-wal", "-shm"))]), 1)
        self.assertEqual(self.put(store, "a1"), "queued")

    def test_quarantine_rename_failure_refuses(self):
        self._garbage()
        with mock.patch.object(spool.os, "replace", side_effect=PermissionError("read-only")), \
                mock.patch.object(spool, "_ERROR_LOG", spool.ErrorLog()), self.assertLogs("alarm_bridge", level="ERROR"), self.assertRaises(SpoolError):
            self.open()

    def test_operational_error_is_not_quarantined(self):
        path = self._garbage()
        with mock.patch.object(spool.sqlite3, "connect", side_effect=sqlite3.OperationalError("database is locked")), \
                mock.patch.object(spool, "_ERROR_LOG", spool.ErrorLog()), self.assertLogs("alarm_bridge", level="ERROR"), self.assertRaises(SpoolError):
            self.open()
        self.assertTrue(os.path.exists(path))
        self.assertFalse([n for n in os.listdir(self.dir) if ".corrupt-" in n])

    def test_open_failure_raises_spoolerror(self):
        with self.assertRaises(SpoolError):
            SpoolStore(os.path.join(self.dir, "nope"), 1024**2, 0, clock=self.clock, disk_usage=self.disk)

    def test_write_failure_raises_spoolerror_and_rate_limits_log(self):
        store = self.open()
        broken = mock.MagicMock()
        broken.execute.side_effect = sqlite3.OperationalError("disk I/O error")
        real, store._conn = store._conn, broken
        try:
            with mock.patch.object(spool, "_ERROR_LOG", spool.ErrorLog()):
                with self.assertLogs("alarm_bridge", level="ERROR") as logs:
                    for i in range(10):
                        with self.assertRaises(SpoolError):
                            self.put(store, f"a{i}")
                errors = [m for m in logs.output if "event=spool_error" in m]
                self.assertEqual(len(errors), 1)
                self.assertIn("count=1", errors[0])
                self.assertIn("OperationalError", errors[0])
                self.clock.advance(61)
                with self.assertLogs("alarm_bridge", level="ERROR") as logs, self.assertRaises(SpoolError):
                    store.depth("slot1")
                errors = [m for m in logs.output if "event=spool_error" in m]
                self.assertEqual(len(errors), 1)
                self.assertIn("count=10", errors[0])
        finally:
            store._conn = real

    def test_disk_usage_failure_is_spoolerror(self):
        def boom(path):
            raise OSError("volume gone")

        store = SpoolStore(self.dir, 1024**2, 0, clock=self.clock, disk_usage=boom)
        self._stores.append(store)
        with self.assertRaises(SpoolError):
            self.put(store, "a1")
        self.assertEqual(store.depth("slot1"), 0)  # rolled back, no half-written row


if __name__ == "__main__":
    unittest.main()
