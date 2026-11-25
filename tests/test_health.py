import socket
import time
import unittest

from alarm_bridge.config import TrapDestination
from alarm_bridge.health import DestinationHealth, tcp_open, udp_refused


class TestUdpRefused(unittest.TestCase):
    def test_udp_refused_true_closed_false_open(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        try:
            self.assertFalse(udp_refused("127.0.0.1", port, wait=0.2))
        finally:
            sock.close()
        self.assertTrue(udp_refused("127.0.0.1", port, wait=0.2))


class TestTcpOpen(unittest.TestCase):
    def test_tcp_open_true_listening_false_closed(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(tcp_open("127.0.0.1", port, timeout=1.0))
        finally:
            srv.close()
        self.assertFalse(tcp_open("127.0.0.1", port, timeout=1.0))


if __name__ == "__main__":
    unittest.main()


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _dest(name="a", tcp=0):
    return TrapDestination(host="192.0.2.1", port=162, community="c", name=name, probe_tcp_port=tcp)


class Probe:
    """Scripted probe: pops results, then repeats the last one. Counts calls."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, host, port):
        self.calls += 1
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r


def _health(dests, udp=None, tcp=None, clock=None, passes=3, reminder=900.0, stats=None):
    return DestinationHealth(
        dests, probe_interval=0.05, up_after_passes=passes, reminder_seconds=reminder,
        spool_stats_fn=stats or (lambda d: {"depth": 4, "oldest_age": 12.0, "drained": 7, "evicted": 2}),
        clock=clock or FakeClock(), udp_probe=udp or Probe(True), tcp_probe=tcp or Probe(True),
    )


class TestDestinationHealth(unittest.TestCase):
    def test_starts_up(self):
        self.assertTrue(_health([_dest()]).is_up("a"))

    def test_mark_down_logs_once(self):
        h = _health([_dest()])
        with self.assertLogs("alarm_bridge", "WARNING") as cm:
            h.mark_down("a", "boom")
            h.mark_down("a", "boom")
        self.assertEqual(len(cm.output), 1)
        self.assertIn("event=dest_down dest=a reason=boom", cm.output[0])
        self.assertFalse(h.is_up("a"))

    def test_mark_down_sanitises_reason(self):
        h = _health([_dest()])
        with self.assertLogs("alarm_bridge", "WARNING") as cm:
            h.mark_down("a", "bad\nline\x00" + "x" * 500)
        self.assertEqual(len(cm.records[0].getMessage().splitlines()), 1)
        self.assertNotIn("\x00", cm.output[0])
        self.assertLess(len(cm.records[0].getMessage()), 200)

    def test_recovers_only_after_n_consecutive_passes(self):
        # udp_probe True == refused == failing round
        udp = Probe(False, True, False, False, False)
        h = _health([_dest()], udp=udp)
        h.mark_down("a", "x")
        states = []
        for _ in range(5):
            h.check_once()
            states.append(h.is_up("a"))
        self.assertEqual(states, [False, False, False, False, True])

    def test_dest_up_log_has_duration_only(self):
        clock = FakeClock()
        totals = {"drained": 0, "evicted": 0}
        h = _health([_dest()], udp=Probe(False), clock=clock, passes=1,
                    stats=lambda d: {"depth": 3, "oldest_age": 0, **totals})
        h.mark_down("a", "x")
        totals.update(drained=7, evicted=2)
        clock.now += 42
        with self.assertLogs("alarm_bridge", "INFO") as cm:
            h.check_once()
        self.assertEqual(len(cm.output), 1)
        self.assertIn("event=dest_up dest=a outage_s=42", cm.output[0])
        self.assertNotIn("drained", cm.output[0])
        self.assertNotIn("evicted", cm.output[0])

    def test_on_up_called_after_recovery_and_its_failure_does_not_block_it(self):
        seen = []
        h = _health([_dest()], udp=Probe(False), passes=1)
        h.on_up = seen.append
        h.mark_down("a", "x")
        h.check_once()
        self.assertEqual(seen, ["a"])
        h2 = _health([_dest()], udp=Probe(False), passes=1)

        def boom(_):
            raise RuntimeError("x")

        h2.on_up = boom
        h2.mark_down("a", "x")
        with self.assertLogs("alarm_bridge", "ERROR") as cm:
            h2.check_once()
        self.assertTrue(h2.is_up("a"))
        self.assertIn("event=on_up_error dest=a", cm.output[0])

    def test_dest_drained_logged_once_when_backlog_empties(self):
        clock = FakeClock()
        totals = {"depth": 5, "drained": 100, "evicted": 10}
        h = _health([_dest()], udp=Probe(False), clock=clock, passes=1,
                    stats=lambda d: {"oldest_age": 0, **totals})
        h.mark_down("a", "x")
        totals["evicted"] += 2
        clock.now += 30
        with self.assertLogs("alarm_bridge", "INFO") as cm:
            h.check_once()
        self.assertFalse([m for m in cm.output if "dest_drained" in m])
        totals.update(depth=0, drained=105)
        clock.now += 4
        with self.assertLogs("alarm_bridge", "INFO") as cm:
            h.backlog_empty("a")
        self.assertEqual(len(cm.output), 1)
        self.assertIn("event=dest_drained dest=a drained=5 evicted=2 drain_s=4", cm.output[0])
        with self.assertNoLogs("alarm_bridge", "INFO"):
            h.backlog_empty("a")  # once per outage

    def test_dest_drained_immediately_when_backlog_already_empty(self):
        clock = FakeClock()
        totals = {"depth": 0, "drained": 0, "evicted": 0}
        h = _health([_dest()], udp=Probe(False), clock=clock, passes=1,
                    stats=lambda d: {"oldest_age": 0, **totals})
        h.mark_down("a", "x")
        totals["evicted"] = 3
        with self.assertLogs("alarm_bridge", "INFO") as cm:
            h.check_once()
        self.assertIn("event=dest_up dest=a", cm.output[0])
        self.assertIn("event=dest_drained dest=a drained=0 evicted=3 drain_s=0", cm.output[1])
        with self.assertNoLogs("alarm_bridge", "INFO"):
            h.backlog_empty("a")

    def test_backlog_empty_without_outage_logs_nothing(self):
        h = _health([_dest()])
        with self.assertNoLogs("alarm_bridge", "INFO"):
            h.backlog_empty("a")

    def test_dest_drained_reports_per_outage_counters_not_lifetime(self):
        totals = {"depth": 1, "drained": 100, "evicted": 10}
        h = _health([_dest()], udp=Probe(False), passes=1,
                    stats=lambda d: {"oldest_age": 0, **totals})
        lines = []
        for drained, evicted in ((5, 1), (3, 0)):
            with self.assertLogs("alarm_bridge", "INFO") as cm:
                h.mark_down("a", "x")
                totals["evicted"] += evicted
                h.check_once()
                totals["drained"] += drained
                h.backlog_empty("a")
            lines.append(cm.output[-1])
        self.assertIn("event=dest_drained dest=a drained=5 evicted=1", lines[0])
        self.assertIn("event=dest_drained dest=a drained=3 evicted=0", lines[1])

    def test_down_again_before_drained_keeps_first_outage_baseline(self):
        totals = {"depth": 4, "drained": 0, "evicted": 0}
        h = _health([_dest()], udp=Probe(False), passes=1,
                    stats=lambda d: {"oldest_age": 0, **totals})
        with self.assertLogs("alarm_bridge", "INFO") as cm:
            h.mark_down("a", "x")
            h.check_once()
            totals["drained"] = 2  # part of the backlog went out, then the destination failed again
            h.mark_down("a", "y")
            h.check_once()
            totals.update(depth=0, drained=4)
            h.backlog_empty("a")
        drained = [m for m in cm.output if "dest_drained" in m]
        self.assertEqual(len(drained), 1)
        self.assertIn("drained=4", drained[0])

    def test_stats_failure_does_not_block_flip_or_dest_up(self):
        def boom(dest_id):
            raise RuntimeError("stats gone")

        h = _health([_dest()], udp=Probe(False), passes=1, stats=boom)
        with self.assertLogs("alarm_bridge", "INFO") as cm:
            h.mark_down("a", "x")
            h.check_once()
        self.assertTrue(h.is_up("a"))
        self.assertIn("event=dest_up dest=a outage_s=0", cm.output[-1])
        with self.assertLogs("alarm_bridge", "INFO") as cm:
            h.backlog_empty("a")
        self.assertIn("event=dest_drained dest=a drained=0 evicted=0", cm.output[-1])

    def test_reminder_every_interval_includes_depth_and_age(self):
        clock = FakeClock()
        h = _health([_dest()], udp=Probe(True), clock=clock)
        h.mark_down("a", "x")
        with self.assertNoLogs("alarm_bridge", "INFO"):
            clock.now += 899
            h.check_once()
        with self.assertLogs("alarm_bridge", "INFO") as cm:
            clock.now += 2
            h.check_once()
            h.check_once()
            clock.now += 901
            h.check_once()
        reminders = [m for m in cm.output if "event=dest_still_down" in m]
        self.assertEqual(len(reminders), 2)
        self.assertIn("dest=a spooled=4 oldest_age_s=12", reminders[0])

    def test_up_destination_with_tcp_port_marked_down_when_tcp_fails(self):
        tcp = Probe(False)
        h = _health([_dest(tcp=8080)], tcp=tcp)
        with self.assertLogs("alarm_bridge", "WARNING"):
            h.check_once()
        self.assertFalse(h.is_up("a"))

    def test_udp_probe_not_run_for_up_destination(self):
        udp, tcp = Probe(True), Probe(True)
        h = _health([_dest(tcp=8080), _dest("b")], udp=udp, tcp=tcp)
        h.check_once()
        self.assertEqual(udp.calls, 0)
        self.assertEqual(tcp.calls, 1)  # only the dest with a tcp port

    def test_slots_independent(self):
        h = _health([_dest("a"), _dest("b")])
        with self.assertLogs("alarm_bridge", "WARNING"):
            h.mark_down("a", "x")
        self.assertFalse(h.is_up("a"))
        self.assertTrue(h.is_up("b"))

    def test_probe_oserror_counts_as_failed_round(self):
        udp = Probe(False, OSError("unreachable"), False)
        h = _health([_dest()], udp=udp, passes=2)
        h.mark_down("a", "x")
        for _ in range(3):
            h.check_once()
        self.assertFalse(h.is_up("a"))
        h.check_once()
        self.assertTrue(h.is_up("a"))

    def test_tcp_probe_oserror_on_up_dest_marks_down(self):
        h = _health([_dest(tcp=8080)], tcp=Probe(OSError("nope")))
        with self.assertLogs("alarm_bridge", "WARNING"):
            h.check_once()
        self.assertFalse(h.is_up("a"))

    def test_thread_survives_unexpected_exception_and_stops_promptly(self):
        h = _health([_dest(tcp=8080)], tcp=Probe(ValueError("bug"), True))
        with self.assertLogs("alarm_bridge", "ERROR"):
            h.start()
            time.sleep(0.3)
        thread = h._thread
        assert thread is not None
        self.assertTrue(thread.is_alive())
        t0 = time.monotonic()
        h.stop()
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertFalse(thread.is_alive())
