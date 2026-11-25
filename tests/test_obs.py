import logging
import socket
import threading
import time
import unittest
from typing import ClassVar
from unittest import mock

from alarm_bridge.config import TrapDestination
from alarm_bridge.obs import RateLimitedEvent, Stats


def make(mode="disk", dests=("a.example", "b.example")):
    ticks = iter([0, 60] + [60] * 100)
    return Stats(list(dests), lambda: mode, clock=lambda: next(ticks))


class StatsTests(unittest.TestCase):
    def test_counters_roll_into_one_summary_and_reset(self):
        s = make()
        for _ in range(5):
            s.received()
        for _ in range(3):
            s.delivered()
        s.duplicate()
        s.duplicate()
        s.undedupable()
        s.rejected()
        s.inflight_timeout()
        s.send_ok("a.example")
        s.send_failed("b.example", "OSError: boom")
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.flush()
        msgs = [r.getMessage() for r in cm.records]
        summary = [m for m in msgs if m.startswith("event=summary")]
        self.assertEqual(len(summary), 1)
        self.assertEqual(
            summary[0],
            "event=summary window=60s received=5 delivered=3 duplicates=2 dup_pct=40 "
            "undedupable=1 rejected=1 auth_failed=0 send_failed=1 inflight_timeouts=1 dedup_store=disk",
        )
        self.assertNotIn("first_reject", summary[0])  # no reason given: line unchanged
        self.assertIn("event=dest dest=a.example ok=1 failed=0 last=ok", msgs)
        self.assertIn("event=dest dest=b.example ok=0 failed=1 last=OSError: boom", msgs)
        s.send_ok("b.example")  # recovers; logs the state change outside the check
        with self.assertNoLogs("alarm_bridge", level="DEBUG"):
            s.flush()

    def test_auth_failures_counted_and_warned_once_per_window(self):
        s = make()
        for _ in range(3):
            s.auth_failed()
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.flush()
        msgs = [(r.levelno, r.getMessage()) for r in cm.records]
        self.assertTrue(any("auth_failed=3" in m for _, m in msgs if m.startswith("event=summary")))
        warns = [m for lv, m in msgs if lv == logging.WARNING]
        self.assertEqual(warns, ["event=auth_failing window=60s auth_failed=3"])
        with self.assertNoLogs("alarm_bridge", level="DEBUG"):
            s.flush()  # counter reset, nothing else happened

    def test_no_auth_warning_when_no_failures(self):
        s = make()
        s.received()
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.flush()
        self.assertFalse(any("auth_failing" in r.getMessage() for r in cm.records))

    def test_same_host_different_ports_are_separate_records(self):
        s = make()
        s.send_failed("h.example:162", "OSError: x")
        for _ in range(3):
            s.send_ok("h.example:1162")
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.flush()
        msgs = [r.getMessage() for r in cm.records]
        self.assertIn("event=dest dest=h.example:162 ok=0 failed=1 last=OSError: x", msgs)
        self.assertIn("event=dest dest=h.example:1162 ok=3 failed=0 last=ok", msgs)

    def test_silent_window_emits_nothing(self):
        with self.assertNoLogs("alarm_bridge", level="DEBUG"):
            make().flush()

    def test_failing_destination_summarised_even_if_counters_zero_next_window(self):
        s = make()
        s.send_failed("a.example", "OSError: x")
        with self.assertLogs("alarm_bridge", level="INFO"):
            s.flush()
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.flush()
        self.assertTrue(any("last=OSError: x" in r.getMessage() for r in cm.records))

    def test_failure_state_logged_once_then_recovery(self):
        s = make()
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            for _ in range(100):
                s.send_failed("a.example", "OSError: down")
        self.assertEqual(len(cm.records), 1)
        self.assertEqual(cm.records[0].levelno, logging.WARNING)
        self.assertEqual(
            cm.records[0].getMessage(),
            "event=dest_state dest=a.example state=FAILING reason=OSError: down",
        )
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.send_ok("a.example")
            s.send_ok("a.example")
        self.assertEqual(len(cm.records), 1)
        self.assertEqual(cm.records[0].levelno, logging.INFO)
        self.assertEqual(cm.records[0].getMessage(), "event=dest_state dest=a.example state=OK")

    def test_reason_truncated(self):
        s = make()
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.send_failed("a.example", "E: " + "x" * 500)
        reason = cm.records[0].getMessage().split("reason=", 1)[1]
        self.assertEqual(len(reason), 200)

    def _storm(self, n, dups):
        s = make()
        for _ in range(n):
            s.received()
        for _ in range(dups):
            s.duplicate()
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.flush()
        return [r for r in cm.records if "event=duplicate_storm" in r.getMessage()]

    def test_storm_threshold(self):
        self.assertEqual(self._storm(19, 19), [])
        self.assertEqual(self._storm(20, 10), [])
        storm = self._storm(20, 11)
        self.assertEqual(len(storm), 1)
        self.assertEqual(storm[0].levelno, logging.WARNING)

    def test_store_mode_degraded(self):
        s = make("memory-degraded")
        s.received()
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.flush()
        self.assertIn("dedup_store=memory-degraded", cm.records[0].getMessage())

    def test_no_exc_info(self):
        s = make()
        s.received()
        s.send_failed("a.example", "OSError: x")
        with self.assertLogs("alarm_bridge", level="DEBUG") as cm:
            s.flush()
            s.send_ok("a.example")
            s.check_destinations([TrapDestination("nonexistent.invalid", 162, "c")])
        for r in cm.records:
            self.assertFalse(r.exc_info)

    def test_start_stop(self):
        s = make()
        s.start(interval=0.01)
        s.stop()
        self.assertIsNotNone(s._thread)
        self.assertFalse(s._thread.is_alive())  # pyright: ignore[reportOptionalMemberAccess]

    def test_flush_error_is_logged_and_summary_thread_survives(self):
        s = make()
        calls = []

        def flaky_flush():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("secret-detail")

        with mock.patch.object(s, "flush", flaky_flush), self.assertLogs("alarm_bridge", level="ERROR") as cm:
            s.start(interval=0.01)
            deadline = time.monotonic() + 3
            while len(calls) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            s.stop()
        self.assertGreaterEqual(len(calls), 3)  # kept flushing after the failure
        self.assertEqual(cm.output[0], "ERROR:alarm_bridge:event=summary_error error=RuntimeError")
        self.assertNotIn("secret-detail", "".join(cm.output))
        self.assertIsNone(cm.records[0].exc_info)

    def test_stop_before_start_is_harmless(self):
        s = make()
        s.stop()
        self.assertIsNone(s._thread)


class FirstRejectReasonTests(unittest.TestCase):
    def _summary(self, s):
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.flush()
        return next(r.getMessage() for r in cm.records if r.getMessage().startswith("event=summary"))

    def test_first_reason_of_the_window_is_in_the_summary(self):
        s = Stats([], lambda: "disk")
        s.rejected("invalid_json")
        s.rejected("alarmSeverity unknown")
        line = self._summary(s)
        self.assertIn("rejected=2", line)
        self.assertIn("first_reject='invalid_json'", line)
        s.received()
        self.assertNotIn("first_reject", self._summary(s))  # window reset, no rejects

    def test_reason_is_one_short_line(self):
        s = Stats([], lambda: "disk")
        s.rejected("bad\nvalue " + "x" * 500)
        line = self._summary(s)
        self.assertNotIn("\n", line)
        self.assertLess(len(line), 400)
        self.assertIn("...", line)


class RateLimitedEventTests(unittest.TestCase):
    def test_first_at_once_then_once_per_interval_per_destination_with_count(self):
        now = [100.0]
        ev = RateLimitedEvent("drain_error", interval=60, clock=lambda: now[0])
        with self.assertLogs("alarm_bridge", "WARNING") as cm:
            ev.note("slot1", "boom")
            ev.note("slot1", "boom")
            ev.note("slot2", "other")
            now[0] += 60
            ev.note("slot1", "boom\nforged line")
        self.assertEqual(len(cm.output), 3)
        self.assertIn("event=drain_error dest=slot1 count=1 reason=boom", cm.output[0])
        self.assertIn("event=drain_error dest=slot2 count=1 reason=other", cm.output[1])
        self.assertIn("event=drain_error dest=slot1 count=2 reason=boom forged line", cm.output[2])
        self.assertEqual(ev.total("slot1"), 3)
        self.assertEqual(ev.total("slot3"), 0)


class RateLimitedEventFlushTests(unittest.TestCase):
    class FakeTimer:
        made: ClassVar[list] = []

        def __init__(self, delay, fn, args=()):
            self.delay, self.fn, self.args, self.daemon, self.started = delay, fn, args, False, False
            self.made.append(self)

        def start(self):
            self.started = True

    def test_held_back_count_is_reported_when_the_window_ends(self):
        self.FakeTimer.made = []
        now = [100.0]
        ev = RateLimitedEvent("drain_error", interval=60, clock=lambda: now[0], timer=self.FakeTimer)
        with self.assertLogs("alarm_bridge", "WARNING") as cm:
            ev.note("slot1", "boom")
            now[0] += 10
            ev.note("slot1", "boom")
            ev.note("slot1", "boom")
            self.assertEqual(len(self.FakeTimer.made), 1)  # one timer per window, not per note
            t = self.FakeTimer.made[0]
            self.assertTrue(t.started and t.daemon)
            self.assertEqual(t.delay, 50)
            now[0] += 50
            t.fn(*t.args)
        self.assertEqual(len(cm.output), 2)
        self.assertIn("event=drain_error dest=slot1 count=2 reason=boom", cm.output[1])

    def test_a_note_after_the_flush_starts_a_new_window(self):
        self.FakeTimer.made = []
        now = [0.0]
        ev = RateLimitedEvent("x", interval=60, clock=lambda: now[0], timer=self.FakeTimer)
        with self.assertLogs("alarm_bridge", "WARNING"):
            ev.note("d", "r")
            now[0] += 5
            ev.note("d", "r")
            t = self.FakeTimer.made[0]
            now[0] += 55
            t.fn(*t.args)
            now[0] += 5
            ev.note("d", "r")
        self.assertEqual(len(self.FakeTimer.made), 2)


class ObserverErrorTests(unittest.TestCase):
    def test_a_failing_observer_is_logged_and_reads_as_no_data(self):
        def boom(_name):
            raise RuntimeError("secret detail")

        s = Stats(["obs-err-dest"], lambda: "disk", spool_fn=boom)
        with self.assertLogs("alarm_bridge", "WARNING") as cm:
            self.assertIsNone(s._call(boom, "obs-err-dest"))
        self.assertIn("event=observer_error dest=obs-err-dest", cm.output[0])
        self.assertIn("RuntimeError", cm.output[0])
        self.assertNotIn("secret detail", cm.output[0])


class CheckDestinationsTests(unittest.TestCase):
    def test_resolvable(self):
        s = make()
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.check_destinations([TrapDestination("localhost", 162, "c", "vna")])
        m = cm.records[0].getMessage()
        self.assertTrue(m.startswith("event=dest_check dest=localhost:162 addr="), m)
        self.assertTrue(m.endswith("scheme=vna"))
        self.assertEqual(cm.records[0].levelno, logging.INFO)

    def test_unresolvable(self):
        s = make()
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.check_destinations([TrapDestination("nonexistent.invalid", 162, "c", "vna")])
        self.assertEqual(cm.records[0].levelno, logging.ERROR)
        self.assertIn("event=dest_check dest=nonexistent.invalid:162 error=", cm.records[0].getMessage())

class SpoolFieldTests(unittest.TestCase):
    def _flush(self, **kw):
        ticks = iter([0, 60] + [60] * 100)
        s = Stats(["a"], lambda: "disk", clock=lambda: next(ticks), **kw)
        s.received()
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.flush()
        return [r.getMessage() for r in cm.records]

    def test_dest_line_includes_spool_fields(self):
        msgs = self._flush(
            spool_fn=lambda d: {"depth": 7, "oldest_age": 12.6, "evicted": 2, "drained": 5}
            if d == "a" else {},
            state_fn=lambda d: False,
        )
        [line] = [m for m in msgs if m.startswith("event=dest ")]
        self.assertEqual(
            line,
            "event=dest dest=a ok=0 failed=0 last=ok state=down spooled=7 oldest_age_s=13 "
            "evicted=2 drained=5",
        )

    def test_dest_line_missing_keys_read_as_zero_and_up(self):
        msgs = self._flush(spool_fn=lambda d: {}, state_fn=lambda d: True)
        [line] = [m for m in msgs if m.startswith("event=dest ")]
        self.assertTrue(line.endswith("state=up spooled=0 oldest_age_s=0 evicted=0 drained=0"), line)

    def test_summary_includes_spool_bytes_and_errors(self):
        msgs = self._flush(
            spool_fn=lambda d: {"bytes": 4096, "errors": 3} if d is None else {},
        )
        [line] = [m for m in msgs if m.startswith("event=summary")]
        self.assertTrue(line.endswith("dedup_store=disk spool_bytes=4096 spool_errors=3"), line)

    def test_failing_spool_fn_does_not_break_flush(self):
        def boom(d):
            raise RuntimeError("x")

        msgs = self._flush(spool_fn=boom, state_fn=boom)
        self.assertTrue(any(m.startswith("event=summary") for m in msgs))


class RecoveryAndQuietWindowTests(unittest.TestCase):
    def _stats(self, **kw):
        ticks = iter([0] + [60] * 100)
        return Stats(["a"], lambda: "disk", clock=lambda: next(ticks), **kw)

    def test_recovered_resets_last_and_logs_ok_once(self):
        s = self._stats()
        s.send_failed("a", "DestinationRefused")
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.recovered("a")
            s.recovered("a")
        self.assertEqual([r.getMessage() for r in cm.records], ["event=dest_state dest=a state=OK"])
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.received()
            s.flush()
        self.assertTrue(any("event=dest dest=a" in r.getMessage() and "last=ok" in r.getMessage()
                            for r in cm.records))

    def test_quiet_window_still_reports_a_down_destination(self):
        s = self._stats(state_fn=lambda d: False, spool_fn=lambda d: {})
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.flush()
        self.assertTrue(any("event=dest dest=a" in r.getMessage() and "state=down" in r.getMessage()
                            for r in cm.records))

    def test_quiet_window_still_reports_a_backlog_on_an_up_destination(self):
        s = self._stats(state_fn=lambda d: True, spool_fn=lambda d: {"depth": 3} if d == "a" else {})
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            s.flush()
        self.assertTrue(any("spooled=3" in r.getMessage() for r in cm.records))

    def test_quiet_window_with_up_empty_destination_stays_silent(self):
        s = self._stats(state_fn=lambda d: True, spool_fn=lambda d: {})
        with self.assertNoLogs("alarm_bridge", level="DEBUG"):
            s.flush()


class SetupLoggingTests(unittest.TestCase):
    """setup_logging: stderr always, optional non-blocking syslog forwarding."""

    def setUp(self):
        import contextlib
        import io

        from alarm_bridge import obs

        self.obs = obs
        root = logging.getLogger()
        self._saved = (root.level, list(root.handlers))
        self.stderr = io.StringIO()
        # StreamHandler binds sys.stderr when created; capture it so 200-line tests stay quiet.
        redirect = contextlib.redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.addCleanup(self._restore)
        self.log = logging.getLogger("alarm_bridge.test")

    def _restore(self):
        self.obs.shutdown_logging()
        root = logging.getLogger()
        root.setLevel(self._saved[0])
        root.handlers[:] = self._saved[1]

    def _ours(self):
        return [h for h in logging.getLogger().handlers if h not in self._saved[1]]

    def _timed_burst(self, line="x"):
        start = time.monotonic()
        for i in range(200):
            self.log.warning("burst %d %s", i, line)
        return time.monotonic() - start

    def test_udp_collector_receives_tagged_line(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(3)
        self.obs.setup_logging("INFO", [("127.0.0.1", sock.getsockname()[1])], "udp")
        self.log.info("hello-collector")
        data = sock.recv(65535)
        self.assertIn(b"appneta-ia-bridge", data)
        self.assertIn(b"hello-collector", data)
        self.assertTrue(data.startswith(b"<14>"), data)  # facility user, severity info
        self.assertIn("hello-collector", self.stderr.getvalue())  # stderr unchanged

    def _udp_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(3)
        return sock

    def test_each_syslog_target_receives_event_lines(self):
        a, b = self._udp_listener(), self._udp_listener()
        self.obs.setup_logging(
            "INFO", [("127.0.0.1", a.getsockname()[1]), ("127.0.0.1", b.getsockname()[1])], "udp"
        )
        self.log.warning("event=dest_down dest=x reason=test")
        for sock in (a, b):
            self.assertIn(b"event=dest_down dest=x", sock.recv(65535))
        self.assertEqual(
            len([t for t in threading.enumerate() if t.name.startswith("syslog-forward")]), 2
        )

    def test_one_dead_syslog_target_does_not_block_others_or_requests(self):
        # Target 1 accepts and never reads, so its forwarder stalls in send; target 2 is a
        # healthy TCP collector. The caller must not block and target 2 must still get lines.
        stuck = socket.socket()
        self.addCleanup(stuck.close)
        stuck.bind(("127.0.0.1", 0))
        stuck.listen(1)
        held = []
        threading.Thread(target=lambda: held.append(stuck.accept()[0]), daemon=True).start()
        self.addCleanup(lambda: [c.close() for c in held])
        good = socket.socket()
        self.addCleanup(good.close)
        good.bind(("127.0.0.1", 0))
        good.listen(1)
        good.settimeout(3)
        timeout = mock.patch.object(self.obs, "_SYSLOG_SOCKET_TIMEOUT", 2.0)
        timeout.start()
        self.addCleanup(timeout.stop)
        self.obs.setup_logging(
            "INFO",
            [("127.0.0.1", stuck.getsockname()[1]), ("127.0.0.1", good.getsockname()[1])],
            "tcp",
        )
        self.assertLess(self._timed_burst("y" * 65536), 1.0)
        conn, _ = good.accept()
        self.addCleanup(conn.close)
        conn.settimeout(3)
        self.assertIn(b"burst", conn.recv(65535))

    def test_stalled_forwarder_does_not_starve_another_target(self):
        stalled = threading.Event()
        delivered = threading.Event()

        class Slow(logging.Handler):
            def emit(self, record):
                stalled.set()
                time.sleep(1.5)

        class Fast(logging.Handler):
            def emit(self, record):
                delivered.set()

        made = iter([Slow(), Fast()])
        with mock.patch.object(self.obs, "_SyslogForwarder", lambda *a, **k: next(made)):
            self.obs.setup_logging("INFO", [("slow", 1), ("fast", 2)], "udp")
        start = time.monotonic()
        self.log.warning("hello")
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertTrue(delivered.wait(1.0))
        self.assertTrue(stalled.wait(1.0))

    def test_unset_address_installs_only_stderr_handler(self):
        self.obs.setup_logging("INFO")
        [handler] = self._ours()
        self.assertIs(type(handler), logging.StreamHandler)
        self.assertEqual(logging.getLogger().level, logging.INFO)

    def test_setup_is_idempotent_and_stops_old_listener(self):
        self.obs.setup_logging("INFO", [("127.0.0.1", 9)], "udp")
        self.assertEqual(len(self._ours()), 2)
        threads = [t for t in threading.enumerate() if t.name == "syslog-forward"]
        self.assertEqual(len(threads), 1)
        self.obs.setup_logging("DEBUG")
        self.assertEqual(len(self._ours()), 1)
        threads[0].join(timeout=5)
        self.assertFalse(threads[0].is_alive())
        self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_unresolvable_collector_does_not_block_or_raise(self):
        for proto in ("udp", "tcp"):
            with self.subTest(proto=proto):
                self.obs.setup_logging("INFO", [("nonexistent.invalid", 1514)], proto)
                self.assertLess(self._timed_burst(), 1.0)

    def test_closed_tcp_port_does_not_block_or_raise_and_warns_once(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.obs.setup_logging("INFO", [("127.0.0.1", port)], "tcp")
        self.assertLess(self._timed_burst(), 1.0)
        deadline = time.monotonic() + 3
        while "event=log_forward_failed" not in self.stderr.getvalue() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.stderr.getvalue().count("event=log_forward_failed"), 1)

    def test_tcp_listener_that_never_reads_does_not_block_or_raise(self):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        held = []
        acceptor = threading.Thread(target=lambda: held.append(server.accept()[0]), daemon=True)
        acceptor.start()
        self.addCleanup(lambda: [c.close() for c in held])
        timeout = mock.patch.object(self.obs, "_SYSLOG_SOCKET_TIMEOUT", 0.3)
        timeout.start()
        self.addCleanup(timeout.stop)
        self.obs.setup_logging("INFO", [("127.0.0.1", server.getsockname()[1])], "tcp")
        # 200 x 64 KiB far exceeds the socket buffers, so the forwarder's sends stall.
        self.assertLess(self._timed_burst("y" * 65536), 1.0)
        # Prove the stall happened: the forwarder's send timed out and it reported that once.
        deadline = time.monotonic() + 3
        while "event=log_forward_failed" not in self.stderr.getvalue() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("timed out", self.stderr.getvalue())
        self.assertEqual(self.stderr.getvalue().count("event=log_forward_failed"), 1)

    def test_full_queue_drops_oldest_and_keeps_newest(self):
        import queue

        q = queue.Queue(maxsize=3)
        handler = self.obs._DropOldestQueueHandler(q)  # no listener: a stalled consumer
        for i in range(10):
            handler.handle(logging.LogRecord("t", logging.INFO, __file__, 1, "line %d", (i,), None))
        kept = [q.get_nowait().getMessage() for _ in range(q.qsize())]
        self.assertEqual(kept, ["line 7", "line 8", "line 9"])

    def test_forwarding_warning_is_rate_limited_to_one_per_minute(self):
        now = [0.0]
        warnings = []
        fwd = self.obs._SyslogForwarder(
            "nonexistent.invalid", 1514, "udp", warn=warnings.append, clock=lambda: now[0]
        )
        self.addCleanup(fwd.close)
        rec = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
        for t in (0, 1, 30, 59.9):
            now[0] = t
            fwd._retry_at = 0  # force a real attempt each time
            fwd.handle(rec)
        self.assertEqual(len(warnings), 1)
        now[0] = 60.0
        fwd._retry_at = 0
        fwd.handle(rec)
        self.assertEqual(len(warnings), 2)
        self.assertIn("event=log_forward_failed", warnings[0])

    def test_tcp_forwarder_keeps_one_record_on_one_line(self):
        # TCP collectors frame on newline; a CR or NUL inside a logged value must not split or cut the record.
        sent = []

        class FakeSock:
            def sendall(self, data):
                sent.append(data)

            def close(self):
                pass

        fwd = self.obs._SyslogForwarder("127.0.0.1", 1514, "tcp", warn=lambda _m: None)
        self.addCleanup(fwd.close)
        fwd.socket = FakeSock()  # pyright: ignore[reportAttributeAccessIssue]
        fwd.handle(logging.LogRecord("t", logging.INFO, __file__, 1, "a\rb\x00c\nd", None, None))
        self.assertEqual(len(sent), 1)
        body = sent[0].decode()
        self.assertEqual(body.count("\n"), 1)
        self.assertTrue(body.endswith("\n"))
        self.assertNotIn("\r", body)
        self.assertNotIn("\x00", body)

    def test_queue_handler_gives_up_after_three_attempts_without_raising(self):
        class AlwaysFull:
            def __init__(self):
                self.puts = 0

            def put_nowait(self, record):
                import queue

                self.puts += 1
                raise queue.Full

            def get_nowait(self):
                return None

        q = AlwaysFull()
        handler = self.obs._DropOldestQueueHandler(q)  # pyright: ignore[reportArgumentType]
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
        handler.handle(record)  # must neither raise nor spin forever
        self.assertEqual(q.puts, 3)

    def test_queue_handler_swallows_errors(self):
        import io

        class Boom:
            def put_nowait(self, record):
                raise ValueError("boom")

        handler = self.obs._DropOldestQueueHandler(Boom())  # pyright: ignore[reportArgumentType]
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
        with mock.patch("sys.stderr", io.StringIO()) as err:
            handler.handle(record)
        self.assertEqual(err.getvalue(), "")  # logging's default handleError would print a traceback

    def test_forward_failure_warning_respects_root_level(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.obs.setup_logging("ERROR", [("127.0.0.1", port)], "tcp")
        self.log.error("forwarder will fail on this line")
        forwarder = self.obs._installed["listeners"][0]._handler
        deadline = time.monotonic() + 3
        while forwarder._last_warn is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(forwarder._last_warn)  # the failure happened and was handled
        self.assertNotIn("event=log_forward_failed", self.stderr.getvalue())  # WARNING is below ERROR


class ForwardListenerShutdownTests(unittest.TestCase):
    """shutdown_logging must forward what is already queued (the final summary), within a bound."""

    def test_queued_records_are_forwarded_before_stop(self):
        import queue

        from alarm_bridge import obs

        handled = []
        gate = threading.Event()

        class SlowHandler(logging.Handler):
            def handle(self, record):
                gate.wait(2)
                handled.append(record)
                return True

        q = queue.Queue()
        listener = obs._ForwardListener(q, SlowHandler())
        listener.start()
        for i in range(3):
            q.put(logging.makeLogRecord({"msg": f"line {i}"}))
        listener.request_stop()
        gate.set()
        listener.finish(5)
        self.assertEqual([r.getMessage() for r in handled], ["line 0", "line 1", "line 2"])

    def test_shutdown_logging_shares_one_deadline_across_targets(self):
        from alarm_bridge import obs

        release = threading.Event()
        self.addCleanup(release.set)
        stuck = []
        for _ in range(3):
            listener = mock.Mock()
            listener._thread = threading.Thread(target=release.wait, args=(30,), daemon=True)
            listener._thread.start()
            listener.finish.side_effect = lambda timeout, t=listener._thread: t.join(timeout)
            stuck.append(listener)
        saved = obs._installed["listeners"]
        obs._installed["listeners"] = stuck
        self.addCleanup(obs._installed.__setitem__, "listeners", saved)
        start = time.monotonic()
        obs.shutdown_logging(timeout=1)
        self.assertLess(time.monotonic() - start, 1.8)
        for listener in stuck:
            listener.request_stop.assert_called_once()
            listener.finish.assert_called_once()


class ReasonSanitizingTests(unittest.TestCase):
    def test_send_failed_reason_stays_one_line(self):
        stats = make()
        nasty = "boom\r\nINFO event=forged  x=1\tend"
        with self.assertLogs("alarm_bridge", level="DEBUG") as cm:
            stats.send_failed("h:162", nasty)
        msg = cm.records[0].getMessage()
        self.assertNotIn("\r", msg)
        self.assertNotIn("\n", msg)
        self.assertNotIn("  ", msg)
        self.assertIn("reason=boom INFO event=forged x=1 end", msg)


if __name__ == "__main__":
    unittest.main()
