import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from alarm_bridge import dedup
from alarm_bridge.dedup import DB_FILENAME, DedupStore, KeyedLocks, dedup_key


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestDedupKey(unittest.TestCase):
    def test_raised_uses_raised_time(self):
        payload = {"alarmId": "a1", "state": "RAISED", "raisedTime": "t-raise", "clearedTime": "t-clear"}
        self.assertEqual(dedup_key(payload), ("a1", "RAISED", "t-raise"))

    def test_cleared_uses_cleared_time(self):
        payload = {"alarmId": "a1", "state": "CLEARED", "raisedTime": "t-raise", "clearedTime": "t-clear"}
        self.assertEqual(dedup_key(payload), ("a1", "CLEARED", "t-clear"))

    def test_list_payload_is_none(self):
        self.assertIsNone(dedup_key([{"alarmId": "a1", "state": "RAISED", "raisedTime": "t"}]))

    def test_integer_alarm_id_is_none(self):
        self.assertIsNone(dedup_key({"alarmId": 7, "state": "RAISED", "raisedTime": "t"}))

    def test_missing_timestamp_is_none(self):
        self.assertIsNone(dedup_key({"alarmId": "a1", "state": "RAISED"}))
        self.assertIsNone(dedup_key({"alarmId": "a1", "state": "CLEARED", "raisedTime": "t"}))

    def test_257_char_value_is_none(self):
        long_value = "x" * 257
        self.assertIsNone(dedup_key({"alarmId": long_value, "state": "RAISED", "raisedTime": "t"}))
        self.assertIsNone(dedup_key({"alarmId": "a1", "state": "RAISED", "raisedTime": long_value}))
        ok = "x" * 256
        self.assertEqual(dedup_key({"alarmId": ok, "state": "RAISED", "raisedTime": ok}), (ok, "RAISED", ok))

    def test_missing_or_unknown_state_is_none(self):
        self.assertIsNone(dedup_key({"alarmId": "a1", "raisedTime": "t"}))
        self.assertIsNone(dedup_key({"alarmId": "a1", "state": "OPEN", "raisedTime": "t"}))

    def test_never_raises_on_any_json_value(self):
        values = [None, 0, 1.5, True, "", "str", [], [1, "a"], {}, {"a": {"b": [1]}},
                  {"alarmId": ["a"], "state": "RAISED", "raisedTime": "t"},
                  {"alarmId": "a", "state": ["RAISED"], "raisedTime": "t"},
                  {"alarmId": "a", "state": {"x": 1}, "raisedTime": "t"},
                  {"alarmId": "a", "state": "RAISED", "raisedTime": None},
                  {"alarmId": "a", "state": "RAISED", "raisedTime": {"n": [1]}},
                  {"alarmId": None, "state": None}]
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(dedup_key(value))


class _Contract:
    """Holder so the loader does not run the shared contract on its own."""

    class StoreContract(unittest.TestCase):
        """Behaviour shared by memory and SQLite stores."""

        clock: FakeClock

        def make_store(self, ttl_hours=96, max_entries=100000) -> DedupStore:
            raise NotImplementedError

        def test_record_then_seen(self):
            store = self.make_store()
            key = ("a1", "RAISED", "t")
            self.assertFalse(store.seen(key))
            store.record(key)
            self.assertTrue(store.seen(key))
            self.assertFalse(store.seen(("a1", "CLEARED", "t")))

        def test_entry_expires_after_ttl(self):
            store = self.make_store(ttl_hours=2)
            key = ("a1", "RAISED", "t")
            store.record(key)
            self.clock.advance(2 * 3600 - 1)
            self.assertTrue(store.seen(key))
            self.clock.advance(3600 + 1)
            self.assertFalse(store.seen(key))

        def test_max_entries_evicts_oldest(self):
            store = self.make_store(max_entries=100)
            with self.assertLogs("alarm_bridge", level="WARNING") as logs:
                for i in range(101):
                    store.record((f"a{i}", "RAISED", "t"))
                    self.clock.advance(1)
            self.assertFalse(store.seen(("a0", "RAISED", "t")))
            for i in range(1, 101):
                self.assertTrue(store.seen((f"a{i}", "RAISED", "t")), i)
            self.assertTrue(any("evict" in line for line in logs.output))

        def test_eviction_warning_is_rate_limited(self):
            # At the cap every new key evicts one; that must not become a log line per event.
            store = self.make_store(max_entries=100)
            for i in range(100):
                store.record((f"a{i}", "RAISED", "t"))
            with self.assertLogs("alarm_bridge", level="WARNING") as logs:
                for i in range(100, 150):
                    store.record((f"a{i}", "RAISED", "t"))
            self.assertEqual(len(logs.records), 1)
            self.assertIn("evicted=1 ", logs.output[0])
            self.clock.advance(61)
            with self.assertLogs("alarm_bridge", level="WARNING") as logs:
                store.record(("a150", "RAISED", "t"))
            self.assertEqual(len(logs.records), 1)
            self.assertIn("evicted=50 ", logs.output[0])

        def test_close_logs_the_eviction_count_still_pending(self):
            store = self.make_store(max_entries=100)
            for i in range(100):
                store.record((f"a{i}", "RAISED", "t"))
            with self.assertLogs("alarm_bridge", level="WARNING") as logs:
                for i in range(100, 103):  # the first eviction logs, the other two wait for the window
                    store.record((f"a{i}", "RAISED", "t"))
            self.assertEqual(len(logs.records), 1)
            with self.assertLogs("alarm_bridge", level="WARNING") as logs:
                store.close()
            self.assertEqual(len(logs.records), 1)
            self.assertIn("evicted=2 ", logs.output[0])


class TestMemoryStore(_Contract.StoreContract):
    def setUp(self):
        self.clock = FakeClock()

    def make_store(self, ttl_hours=96, max_entries=100000):
        store = DedupStore("", ttl_hours, max_entries, clock=self.clock)
        self.addCleanup(store.close)
        self.assertEqual(store.mode, "memory")
        return store

    def test_purge_drops_only_expired_entries(self):
        store = self.make_store(ttl_hours=1)
        store.record(("old", "RAISED", "t"))
        self.clock.advance(1800)
        store.record(("new", "RAISED", "t"))
        self.clock.advance(1801)  # old is 3601 s, new is 1801 s
        store._purge()
        self.assertEqual(list(store._memory), [("new", "RAISED", "t")])
        self.assertTrue(store.seen(("new", "RAISED", "t")))

    def test_expired_key_seen_again_is_forgotten(self):
        store = self.make_store(ttl_hours=1)
        key = ("a1", "RAISED", "t")
        store.record(key)
        self.clock.advance(3601)
        self.assertFalse(store.seen(key))
        self.assertNotIn(key, store._memory)  # removed on lookup, not left to the purge thread

    def test_background_purge_thread_expires_entries(self):
        with mock.patch.object(dedup, "PURGE_INTERVAL_SECONDS", 0.01):
            store = self.make_store(ttl_hours=1)
        store.record(("old", "RAISED", "t"))
        self.clock.advance(3601)
        deadline = time.monotonic() + 3
        while store._memory and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(store._memory), 0)


class TestSqliteStore(_Contract.StoreContract):
    def setUp(self):
        self.clock = FakeClock()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def make_store(self, ttl_hours=96, max_entries=100000, on_error=None):
        store = DedupStore(self.dir, ttl_hours, max_entries, clock=self.clock, on_error=on_error)
        self.addCleanup(store.close)
        self.assertEqual(store.mode, "sqlite")
        return store

    def _count_rows(self, store):
        assert store._conn is not None
        return store._conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def test_mid_run_corruption_is_renamed_aside_on_the_next_purge_tick(self):
        store = self.make_store()
        store.record(("a1", "RAISED", "t"))
        path = Path(self.dir) / DB_FILENAME

        def corrupt(*args):
            path.write_bytes(b"this is not a database" * 500)
            raise sqlite3.DatabaseError("database disk image is malformed")

        with mock.patch.object(store, "_record_sqlite", side_effect=corrupt):
            store.record(("a2", "RAISED", "t"))
        self.assertEqual(store.mode, "memory-degraded")
        with self.assertLogs("alarm_bridge", level="ERROR") as logs:
            store._purge()  # no restart needed
        self.assertTrue(any("event=dedup_store_corrupt" in line for line in logs.output))
        self.assertEqual(store.mode, "sqlite")
        self.assertEqual(len([p for p in Path(self.dir).iterdir() if ".corrupt-" in p.name]), 1)
        store.record(("a3", "RAISED", "t"))  # the fresh file is in use
        self.assertTrue(store.seen(("a3", "RAISED", "t")))
        self.assertEqual(self._count_rows(store), 1)

    def test_seen_and_record_after_close_do_not_log_error_or_raise(self):
        store = self.make_store()
        store.close()
        with self.assertNoLogs("alarm_bridge", "ERROR"):
            store.record(("a", "RAISED", "t"))
            store.seen(("a", "RAISED", "t"))
        self.assertEqual(store.mode, "sqlite")

    def test_purge_drops_only_expired_rows(self):
        store = self.make_store(ttl_hours=1)
        store.record(("old", "RAISED", "t"))
        self.clock.advance(1800)
        store.record(("new", "RAISED", "t"))
        self.clock.advance(1801)
        store._purge()
        self.assertEqual(self._count_rows(store), 1)
        self.assertTrue(store.seen(("new", "RAISED", "t")))

    def test_record_does_not_count_the_table_and_commits_once(self):
        store = self.make_store(max_entries=3)
        assert store._conn is not None
        statements = []
        store._conn.set_trace_callback(statements.append)
        for i in range(5):  # the last two evict
            store.record((f"a{i}", "RAISED", "t"))
        issued = list(statements)  # before this test's own count query below
        self.assertEqual(self._count_rows(store), 3)
        self.assertFalse([st for st in issued if "COUNT(" in st.upper()])
        self.assertEqual(sum(1 for st in issued if st.upper() == "COMMIT"), 5)

    def test_tracked_count_survives_rerecord_and_purge(self):
        # The cached row count must follow upserts and TTL purges, or the cap would trip early or never.
        store = self.make_store(ttl_hours=1, max_entries=3)
        for i in range(3):
            store.record((f"old{i}", "RAISED", "t"))
        store.record(("old0", "RAISED", "t"))  # re-record: not a new row
        self.clock.advance(3601)
        store._purge()
        for i in range(3):
            store.record((f"new{i}", "RAISED", "t"))
        self.assertEqual(self._count_rows(store), 3)
        for i in range(3):
            self.assertTrue(store.seen((f"new{i}", "RAISED", "t")))

    def test_degraded_store_returns_to_sqlite_and_keeps_its_records(self):
        store = self.make_store()
        assert store._conn is not None
        store._conn.close()
        key = ("a1", "RAISED", "t")
        with self.assertLogs("alarm_bridge", level="ERROR"):
            store.record(key)  # the failing call itself stores nothing (fail open)
        self.assertEqual(store.mode, "memory-degraded")
        store.record(("a2", "RAISED", "t"))  # recorded while degraded, in memory
        store.record(("a3", "RAISED", "t"))
        with self.assertLogs("alarm_bridge", level="WARNING") as logs:
            store._purge()  # the periodic tick retries SQLite
        self.assertEqual(store.mode, "sqlite")
        self.assertTrue(any("event=dedup_store_recovered" in m for m in logs.output))
        self.assertTrue(store.seen(("a2", "RAISED", "t")))
        self.assertTrue(store.seen(("a3", "RAISED", "t")))
        self.assertEqual(self._count_rows(store), 2)

    def test_degraded_store_stays_degraded_while_sqlite_is_still_broken(self):
        store = self.make_store()
        assert store._conn is not None
        store._conn.close()
        with self.assertLogs("alarm_bridge", level="ERROR"):
            store.record(("a1", "RAISED", "t"))
        store.record(("a1", "RAISED", "t"))
        with (
            mock.patch.object(DedupStore, "_connect", side_effect=sqlite3.OperationalError("unable to open")),
            self.assertNoLogs("alarm_bridge", "ERROR"),
        ):
            store._purge()
        self.assertEqual(store.mode, "memory-degraded")
        self.assertTrue(store.seen(("a1", "RAISED", "t")))

    def test_count_failure_at_open_degrades_without_leaving_a_connection(self):
        with (
            mock.patch.object(DedupStore, "_count_rows", side_effect=sqlite3.OperationalError("disk I/O error")),
            self.assertLogs("alarm_bridge", level="ERROR"),
        ):
            store = DedupStore(self.dir, 96, 100000, clock=self.clock)
        self.addCleanup(store.close)
        self.assertEqual(store.mode, "memory-degraded")
        self.assertIsNone(store._conn)

    def test_late_failure_from_a_replaced_connection_leaves_the_new_one_alone(self):
        store = self.make_store()
        old = store._conn
        assert old is not None
        old.close()
        with self.assertLogs("alarm_bridge", level="ERROR"):
            store.record(("a1", "RAISED", "t"))  # degrades
        with self.assertLogs("alarm_bridge", level="WARNING"):
            store._purge()  # recovers onto a new connection
        self.assertEqual(store.mode, "sqlite")
        new = store._conn
        store._fail(sqlite3.OperationalError("late error from the old connection"), old)
        self.assertEqual(store.mode, "sqlite")
        self.assertIs(store._conn, new)

    def test_purge_resyncs_the_tracked_row_count(self):
        store = self.make_store()
        for i in range(3):
            store.record((f"a{i}", "RAISED", "t"))
        store._rows = 999  # drift, however it happened
        store._purge()
        self.assertEqual(store._rows, 3)

    def test_purge_thread_survives_an_unexpected_exception(self):
        calls = []

        def flaky(self_):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("embedded null byte")

        with (
            mock.patch.object(dedup, "PURGE_INTERVAL_SECONDS", 0.01),
            mock.patch.object(DedupStore, "_purge", flaky),
            self.assertLogs("alarm_bridge", level="ERROR") as logs,
        ):
            self.make_store()
            deadline = time.time() + 2
            while len(calls) < 3 and time.time() < deadline:
                time.sleep(0.01)
        self.assertGreaterEqual(len(calls), 3)
        self.assertTrue(any("event=dedup_purge_error" in m for m in logs.output))

    def test_closed_store_does_not_reopen_sqlite(self):
        store = self.make_store()
        store.close()
        store._purge()
        self.assertIsNone(store._conn)

    def test_background_purge_thread_deletes_expired_rows(self):
        with mock.patch.object(dedup, "PURGE_INTERVAL_SECONDS", 0.01):
            store = self.make_store(ttl_hours=1)
        store.record(("old", "RAISED", "t"))
        self.clock.advance(3601)
        deadline = time.monotonic() + 3
        while self._count_rows(store) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self._count_rows(store), 0)

    def test_purge_failure_fails_open_and_reports(self):
        errors = []
        store = self.make_store(on_error=errors.append)
        key = ("a1", "RAISED", "t")
        store.record(key)
        assert store._conn is not None
        store._conn.close()
        with self.assertLogs("alarm_bridge", level="ERROR") as logs:
            store._purge()  # must not raise
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], sqlite3.ProgrammingError)
        self.assertIn("event=dedup_store_error", logs.output[0])

    def test_failure_without_on_error_callback_still_logs(self):
        store = self.make_store()
        assert store._conn is not None
        store._conn.close()
        with self.assertLogs("alarm_bridge", level="ERROR") as logs:
            self.assertFalse(store.seen(("a1", "RAISED", "t")))
        self.assertEqual(len(logs.records), 1)

    def test_locked_database_degrades_without_moving_the_file(self):
        db_path = Path(self.dir) / DB_FILENAME
        db_path.write_bytes(b"")
        locked = sqlite3.OperationalError("database is locked")
        with (
            mock.patch.object(DedupStore, "_connect", side_effect=locked),
            self.assertLogs("alarm_bridge", level="ERROR") as logs,
        ):
            store = DedupStore(self.dir, 96, 100000, clock=self.clock)
        self.addCleanup(store.close)
        self.assertEqual(store.mode, "memory-degraded")
        self.assertIn("OperationalError", logs.output[0])
        self.assertEqual([p.name for p in Path(self.dir).iterdir()], [DB_FILENAME])

    def test_corrupt_file_takes_its_journal_along(self):
        db_path = Path(self.dir) / DB_FILENAME
        db_path.write_bytes(b"this is not a sqlite database at all " * 100)
        journal = Path(self.dir) / (DB_FILENAME + "-journal")
        journal.write_bytes(b"stale journal")
        real_connect = DedupStore._connect
        calls = []

        def connect(store, path):
            calls.append(path)
            if len(calls) == 1:
                raise sqlite3.DatabaseError("file is not a database")  # sqlite itself would eat the stale journal
            return real_connect(store, path)

        with mock.patch.object(DedupStore, "_connect", connect), self.assertLogs("alarm_bridge", level="ERROR"):
            self.make_store()
        names = sorted(p.name for p in Path(self.dir).iterdir())
        asides = [n for n in names if ".corrupt-" in n]
        self.assertEqual(len(asides), 2)
        self.assertTrue(any(n.endswith("-journal") for n in asides))
        self.assertFalse(journal.exists())

    def test_failed_quick_check_is_treated_as_corruption(self):
        real_connect = sqlite3.connect
        bad = mock.MagicMock()
        bad.execute.return_value.fetchone.return_value = ("row 1 missing from index seen_ts",)
        calls = []

        def fake_connect(*args, **kwargs):
            calls.append(args)
            return bad if len(calls) == 1 else real_connect(*args, **kwargs)

        with (
            mock.patch.object(dedup.sqlite3, "connect", side_effect=fake_connect),
            self.assertLogs("alarm_bridge", level="ERROR") as logs,
        ):
            store = self.make_store()
        bad.close.assert_called_once_with()  # the suspect connection is not leaked
        self.assertIn("event=dedup_store_corrupt", logs.output[0])
        self.assertIn("quick_check", logs.output[0])
        self.assertEqual(len([p for p in Path(self.dir).iterdir() if ".corrupt-" in p.name]), 1)
        key = ("a1", "RAISED", "t")
        store.record(key)
        self.assertTrue(store.seen(key))

    def test_reopen_still_sees_key(self):
        key = ("a1", "RAISED", "t")
        first = self.make_store()
        first.record(key)
        first.close()
        second = self.make_store()
        self.assertTrue(second.seen(key))

    def test_reopen_purges_expired_rows(self):
        first = self.make_store(ttl_hours=1)
        first.record(("old", "RAISED", "t"))
        first.close()
        self.clock.advance(3601)
        self.make_store(ttl_hours=1).close()
        conn = sqlite3.connect(os.path.join(self.dir, DB_FILENAME))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0], 0)
        finally:
            conn.close()

    def test_db_file_mode_is_0600(self):
        old_umask = os.umask(0o022)
        try:
            self.make_store().record(("a1", "RAISED", "t"))
        finally:
            os.umask(old_umask)
        mode = stat.S_IMODE(os.stat(os.path.join(self.dir, DB_FILENAME)).st_mode)
        self.assertEqual(mode, 0o600)

    def test_corrupt_file_is_renamed_aside(self):
        db_path = Path(self.dir) / DB_FILENAME
        db_path.write_bytes(b"this is not a sqlite database at all " * 100)
        with self.assertLogs("alarm_bridge", level="ERROR"):
            store = self.make_store()
        aside = [p.name for p in Path(self.dir).iterdir() if p.name.startswith(DB_FILENAME + ".corrupt-")]
        self.assertEqual(len(aside), 1)
        key = ("a1", "RAISED", "t")
        store.record(key)
        self.assertTrue(store.seen(key))

    def test_store_failure_fails_open_and_logs_once_per_minute(self):
        errors = []
        store = self.make_store(on_error=errors.append)
        key = ("a1", "RAISED", "t")
        store.record(key)
        assert store._conn is not None
        store._conn.close()  # every later sqlite call raises sqlite3.ProgrammingError
        exc = sqlite3.ProgrammingError("closed")
        with self.assertLogs("alarm_bridge", level="ERROR") as logs:
            for _ in range(25):
                store._fail(exc)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(len(errors), 25)
        self.assertIsNone(logs.records[0].exc_info)
        self.assertIn("ProgrammingError", logs.output[0])
        self.clock.advance(60)
        with self.assertLogs("alarm_bridge", level="ERROR") as logs:
            store._fail(exc)
        self.assertEqual(len(logs.records), 1)

    def test_runtime_error_switches_to_memory_degraded_and_keeps_deduping(self):
        store = self.make_store()
        conn = store._conn
        assert conn is not None
        conn.close()  # every later sqlite call raises sqlite3.ProgrammingError
        key = ("a1", "RAISED", "t")
        with self.assertLogs("alarm_bridge", level="ERROR"):
            self.assertFalse(store.seen(key))
        self.assertEqual(store.mode, "memory-degraded")
        self.assertIsNone(store._conn)
        store.record(key)
        self.assertTrue(store.seen(key))

    def test_runtime_error_in_record_also_degrades(self):
        store = self.make_store()
        assert store._conn is not None
        store._conn.close()
        key = ("a1", "RAISED", "t")
        with self.assertLogs("alarm_bridge", level="ERROR"):
            store.record(key)
        self.assertEqual(store.mode, "memory-degraded")
        store.record(key)
        self.assertTrue(store.seen(key))

    def test_concurrent_record_and_seen_from_threads(self):
        store = self.make_store()
        failures = []

        def worker(n):
            try:
                for i in range(50):
                    key = (f"t{n}-{i}", "RAISED", "t")
                    store.record(key)
                    if not store.seen(key):
                        failures.append(key)
            except Exception as exc:  # pragma: no cover - surfaced by the assertion below
                failures.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(failures, [])

    def test_close_stops_purge_thread(self):
        store = self.make_store()
        thread = store._purge_thread
        self.assertTrue(thread.daemon)
        self.assertTrue(thread.is_alive())
        store.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        store.close()  # idempotent


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
class TestDegradedStore(unittest.TestCase):
    def test_read_only_dir_degrades_to_memory(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o500)
            try:
                with self.assertLogs("alarm_bridge", level="ERROR") as logs:
                    store = DedupStore(tmp, 96, 100000, clock=clock)
                try:
                    self.assertEqual(store.mode, "memory-degraded")
                    self.assertEqual(len(logs.records), 1)
                    key = ("a1", "RAISED", "t")
                    self.assertFalse(store.seen(key))
                    store.record(key)
                    self.assertTrue(store.seen(key))
                finally:
                    store.close()
            finally:
                os.chmod(tmp, 0o700)


class TestKeyedLocks(unittest.TestCase):
    def test_second_acquire_times_out_while_held(self):
        locks = KeyedLocks()
        key = ("a1", "RAISED", "t")
        self.assertTrue(locks.acquire(key, timeout=0.2))
        result = []
        t = threading.Thread(target=lambda: result.append(locks.acquire(key, timeout=0.2)))
        t.start()
        t.join()
        self.assertEqual(result, [False])
        locks.release(key)
        t = threading.Thread(target=lambda: result.append(locks.acquire(key, timeout=0.2)))
        t.start()
        t.join()
        self.assertEqual(result, [False, True])
        locks.release(key)

    def test_a_held_key_never_blocks_a_different_key(self):
        locks = KeyedLocks()
        self.assertTrue(locks.acquire(("slow", "RAISED", "t"), timeout=0.2))
        result = []
        for i in range(200):  # many keys: with shared stripes some would collide with the held one
            key = (f"other{i}", "RAISED", "t")
            t = threading.Thread(target=lambda k=key: result.append(locks.acquire(k, timeout=0.05)))
            t.start()
            t.join()
            locks.release(key)
        self.assertEqual(result, [True] * 200)
        locks.release(("slow", "RAISED", "t"))

    def test_entries_are_dropped_when_unused(self):
        locks = KeyedLocks()
        key = ("a1", "RAISED", "t")
        self.assertTrue(locks.acquire(key, timeout=0.2))
        t = threading.Thread(target=lambda: locks.acquire(key, timeout=0.1))  # times out
        t.start()
        t.join()
        locks.release(key)
        self.assertEqual(len(locks._entries), 0)


if __name__ == "__main__":
    unittest.main()
