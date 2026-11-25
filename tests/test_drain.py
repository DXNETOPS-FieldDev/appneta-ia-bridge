import json
import os
import socket
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from alarm_bridge import mapping
from alarm_bridge.config import TrapDestination
from alarm_bridge.drain import Drainer
from alarm_bridge.snmp_trap import DestinationRefused, send_trap
from alarm_bridge.spool import SpoolStore

LOTS = 10 * 1024**3
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "alarm_raised.json")


def load_payload(alarm_id="a1"):
    with open(FIXTURE) as fh:
        payload = json.load(fh)
    payload["alarmId"] = alarm_id
    return payload


def key_json(alarm_id, state="RAISED", ts="t"):
    return json.dumps([alarm_id, state, ts])


class FakeHealth:
    def __init__(self, up=True):
        self.up = {}
        self.default = up
        self.down = []
        self.emptied = []

    def is_up(self, dest_id):
        return self.up.get(dest_id, self.default)

    def backlog_empty(self, dest_id):
        self.emptied.append(dest_id)

    def mark_down(self, dest_id, reason):
        self.up[dest_id] = False
        self.down.append((dest_id, reason))


class FakeStore:
    def __init__(self):
        self.recorded = []

    def record(self, key):
        self.recorded.append(key)


class DrainTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.spool = SpoolStore(self._tmp.name, 64 * 1024**2, 0)
        self.dest = TrapDestination("192.0.2.1", 162, "public", slot=1)
        self.dest2 = TrapDestination("192.0.2.2", 162, "public", slot=2, source_address="198.51.100.9")
        self.config = SimpleNamespace(trap_destinations=[self.dest, self.dest2], v3_id_offset=None)
        self.health = FakeHealth()
        self.store = FakeStore()
        self.sent = []
        self.sleeps = []
        self.send_error = None

    def tearDown(self):
        self.spool.close()
        self._tmp.cleanup()

    def sender(self, host, port, community, trap_oid, varbinds):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((host, port, community, trap_oid, list(varbinds)))

    def drainer(self, rate=20, sender=None):
        return Drainer(self.config, self.spool, self.health, self.store, sender or self.sender,
                       rate, sleep=self.sleeps.append)

    def put(self, alarm_id, dest="slot1", state="RAISED", ts="t", payload=None):
        return self.spool.enqueue(key_json(alarm_id, state, ts), dest,
                                  payload if payload is not None else load_payload(alarm_id), alarm_id, state)


class TestDrainer(DrainTestCase):
    def test_drains_oldest_first_and_deletes_after_send(self):
        for i in ("a", "b", "c"):
            self.put(i)
        n = self.drainer().drain_once(self.dest)
        self.assertEqual(n, 3)
        self.assertEqual(self.spool.depth("slot1"), 0)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual([k[0] for k in self.store.recorded], ["a", "b", "c"])

    def test_tells_health_when_backlog_is_empty(self):
        self.put("a")
        self.put("b")
        self.drainer().drain_once(self.dest)
        self.assertEqual(self.health.emptied, ["slot1"])

    def test_backlog_not_reported_empty_while_rows_remain_or_dest_down(self):
        for i in range(3):
            self.put(f"a{i}")
        self.drainer(rate=2).drain_once(self.dest)  # budget spent, one row left
        self.assertEqual(self.health.emptied, [])
        self.health.up["slot1"] = False
        self.drainer().drain_once(self.dest)
        self.assertEqual(self.health.emptied, [])

    def test_send_failure_marks_down_and_keeps_row(self):
        self.put("a")
        self.put("b")
        self.send_error = DestinationRefused("refused")
        d = self.drainer()
        self.assertEqual(d.drain_once(self.dest), 0)
        self.assertEqual(self.spool.depth("slot1"), 2)
        self.assertEqual(len(self.health.down), 1)
        self.assertEqual(self.health.down[0][0], "slot1")
        self.assertEqual(self.store.recorded, [])
        self.assertEqual(d.drained("slot1"), 0)

    def test_generic_exception_marks_down(self):
        self.put("a")
        self.send_error = OSError("boom")
        self.drainer().drain_once(self.dest)
        self.assertEqual(self.spool.depth("slot1"), 1)
        self.assertEqual(len(self.health.down), 1)

    def test_dedup_record_written_after_send(self):
        self.put("a", state="CLEARED", ts="t9")
        d = self.drainer()
        d.drain_once(self.dest)
        self.assertEqual(self.store.recorded, [("a", "CLEARED", "t9", "slot1")])
        self.assertEqual(d.drained("slot1"), 1)

    def test_undedupable_key_not_recorded(self):
        self.spool.enqueue(json.dumps(["undedupable", "x"]), "slot1", load_payload(), "a", "RAISED")
        d = self.drainer()
        self.assertEqual(d.drain_once(self.dest), 1)
        self.assertEqual(self.store.recorded, [])
        self.assertEqual(self.spool.depth("slot1"), 0)
        self.assertEqual(d.drained("slot1"), 1)

    def test_rate_limited_to_budget(self):
        for i in range(30):
            self.put(f"a{i}")
        d = self.drainer(rate=20)
        self.assertEqual(d.drain_once(self.dest), 20)
        self.assertEqual(self.spool.depth("slot1"), 10)
        self.assertTrue(self.sleeps)
        self.assertTrue(all(0 < s <= 1 / 20 for s in self.sleeps))
        self.assertLessEqual(len(self.sleeps), 20)

    def test_unmappable_row_dropped_and_does_not_block_queue(self):
        self.put("bad", payload={"alarmId": "bad"})
        self.put("good")
        d = self.drainer()
        self.assertEqual(d.drain_once(self.dest), 1)
        self.assertEqual(self.spool.depth("slot1"), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(d.dropped_unmappable("slot1"), 1)
        self.assertEqual(self.health.down, [])

    def test_poison_row_with_unexpected_mapping_error_is_dropped_not_retried_forever(self):
        self.put("poison")
        self.put("good")
        d = self.drainer()
        real = mapping.map_for_destination

        def flaky(payload, dest, offset):
            if payload["alarmId"] == "poison":
                raise RuntimeError("boom")
            return real(payload, dest, offset)

        with mock.patch.object(mapping, "map_for_destination", flaky):
            self.assertEqual(d.drain_once(self.dest), 1)
        self.assertEqual(self.spool.depth("slot1"), 0)
        self.assertEqual(d.dropped_unmappable("slot1"), 1)
        self.assertEqual(self.health.down, [])

    def test_corrupt_key_json_after_send_still_deletes_the_row(self):
        self.spool.enqueue("not json", "slot1", load_payload("k"), "k", "RAISED")
        d = self.drainer()
        self.assertEqual(d.drain_once(self.dest), 1)
        self.assertEqual(self.spool.depth("slot1"), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store.recorded, [])

    def test_unmappable_log_is_rate_limited_and_has_no_payload(self):
        for i in range(3):
            self.put(f"bad{i}", payload={"alarmId": f"bad{i}", "secret": "SEKRIT"})
        with self.assertLogs("alarm_bridge", level="DEBUG") as cm:
            self.drainer().drain_once(self.dest)
        lines = [m for m in cm.output if "dropped_unmappable" in m]
        self.assertEqual(len(lines), 1)
        self.assertNotIn("SEKRIT", lines[0])

    def test_only_up_destinations_are_drained(self):
        self.put("a")
        self.health.up["slot1"] = False
        self.assertEqual(self.drainer().drain_once(self.dest), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.spool.depth("slot1"), 1)

    def test_source_address_varbind_appended(self):
        self.put("a", dest="slot2")
        self.drainer().drain_once(self.dest2)
        self.assertIn(("198.51.100.9", "ipaddress"), [(v, t) for _, v, t in self.sent[0][4]])

    def test_incident_shape_one_send_each(self):
        for _ in range(33):
            for i in range(23):
                self.put(f"ev{i}", ts=f"t{i}")
        self.assertEqual(self.spool.depth("slot1"), 23)
        d = self.drainer(rate=1000)
        total = 0
        while self.spool.depth("slot1"):
            total += d.drain_once(self.dest)
        self.assertEqual(total, 23)
        self.assertEqual(len(self.sent), 23)

    def test_crash_between_send_and_delete_resends_at_most_once(self):
        self.put("a")
        calls = []

        def sender(*args):
            calls.append(args)

        d1 = self.drainer(sender=sender)
        real_delete = self.spool.delete
        self.spool.delete = lambda rowid: (_ for _ in ()).throw(RuntimeError("crash"))
        with self.assertRaises(RuntimeError):
            d1.drain_once(self.dest)
        self.spool.delete = real_delete
        self.assertEqual(len(calls), 1)
        d2 = self.drainer(sender=sender)
        d2.drain_once(self.dest)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.spool.depth("slot1"), 0)
        d2.drain_once(self.dest)
        self.assertEqual(len(calls), 2)

    def test_sender_called_without_drainer_lock_held(self):
        self.put("a")
        d = self.drainer()
        held = []

        def sender(*args):
            held.append(d._lock.locked())

        d._sender = sender
        d.drain_once(self.dest)
        self.assertEqual(held, [False])


class TestDrainPoisonAndErrors(DrainTestCase):
    def test_oversized_alarm_dropped_destination_stays_up_and_queue_moves(self):
        # A real send_trap to a local listener: a 100 KB description cannot fit in one datagram.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

            def sender(host, port_, community, trap_oid, varbinds):
                send_trap("127.0.0.1", port, community, trap_oid, varbinds)
                self.sent.append(host)

            big = load_payload("big")
            big["description"] = "x" * 100_000
            self.put("big", payload=big)
            self.put("good")
            d = self.drainer(sender=sender)
            with self.assertLogs("alarm_bridge", "WARNING") as cm:
                self.assertEqual(d.drain_once(self.dest), 1)
        self.assertEqual(self.health.down, [])
        self.assertEqual(self.sent, ["192.0.2.1"])
        self.assertEqual(self.spool.depth("slot1"), 0)
        self.assertEqual([k[0] for k in self.store.recorded], ["big", "good"])  # a retry is not re-spooled
        self.assertEqual(d.drained("slot1"), 1)
        self.assertEqual(d.dropped_unsendable("slot1"), 1)
        line = next(m for m in cm.output if "event=dropped_unsendable" in m)
        self.assertIn("dest=slot1", line)
        self.assertNotIn("xxxx", line)

    def test_non_json_payload_is_dropped_and_the_queue_moves(self):
        self.put("bad")
        self.put("good")
        head = self.spool.oldest("slot1")
        assert head is not None
        bad = head[0]
        self.spool._read(lambda conn: conn.execute("UPDATE spool SET payload = 'not json' WHERE rowid = ?", (bad,)))
        d = self.drainer()
        with self.assertLogs("alarm_bridge", "WARNING"):
            self.assertEqual(d.drain_once(self.dest), 1)
        self.assertEqual(self.spool.depth("slot1"), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.health.down, [])

    def test_one_destination_raising_does_not_stop_the_others(self):
        self.put("b1", dest="slot2")
        self.put("b2", dest="slot2")
        real = self.spool.oldest

        def oldest(dest_id):
            if dest_id == "slot1":
                raise ValueError("Expecting value: line 1 column 1 (char 0)")
            return real(dest_id)

        self.spool.oldest = oldest
        d = self.drainer()
        with self.assertLogs("alarm_bridge", "WARNING") as cm:
            self.assertEqual(d._drain_pass(), 2)
            d._drain_pass()
        self.assertEqual(self.spool.depth("slot2"), 0)
        errors = [m for m in cm.output if "event=drain_error" in m]
        self.assertEqual(len(errors), 1)  # rate limited per destination
        self.assertIn("dest=slot1", errors[0])
        self.assertIn("ValueError", errors[0])
        self.assertNotIn("Traceback", "\n".join(cm.output))


class TestDrainPacing(DrainTestCase):
    """Fake clock and sleep: the send rate is measured from send start times, not wall time."""

    def _starts(self, rate, send_s, count, slow=None):
        clock = {"now": 0.0}
        starts = []

        def sleep(seconds):
            self.sleeps.append(seconds)
            clock["now"] += seconds

        def sender(*args):
            starts.append(clock["now"])
            clock["now"] += (slow or {}).get(len(starts), send_s)

        for i in range(count):
            self.put(f"a{i}")
        d = Drainer(self.config, self.spool, self.health, self.store, sender, rate,
                    sleep=sleep, clock=lambda: clock["now"])
        while d.drain_once(self.dest):
            pass
        self.assertEqual(len(starts), count)
        return starts

    @staticmethod
    def _gaps(starts):
        return [round(b - a, 9) for a, b in zip(starts, starts[1:])]

    def test_send_time_does_not_slow_the_rate(self):
        # 50 ms per send (the refusal wait) at 20/s: the old sleep-after-send pacing gave 10/s.
        starts = self._starts(rate=20, send_s=0.05, count=40)
        self.assertEqual(self._gaps(starts), [0.05] * 39)

    def test_short_send_sleeps_only_the_remainder(self):
        starts = self._starts(rate=20, send_s=0.02, count=10)
        self.assertEqual(self._gaps(starts), [0.05] * 9)
        self.assertTrue(all(abs(s - 0.03) < 1e-9 for s in self.sleeps))

    def test_send_slower_than_interval_goes_back_to_back(self):
        starts = self._starts(rate=20, send_s=0.08, count=10)
        self.assertEqual(self._gaps(starts), [0.08] * 9)
        self.assertFalse([s for s in self.sleeps if s > 0])

    def test_stall_does_not_cause_a_burst(self):
        starts = self._starts(rate=20, send_s=0.01, count=10, slow={3: 2.0})
        gaps = self._gaps(starts)
        self.assertEqual(gaps[2], 2.0)
        self.assertEqual(gaps[3:], [0.05] * 6)


class TestDrainerThread(DrainTestCase):
    def test_thread_drains_and_stops_promptly(self):
        self.put("a")
        done = threading.Event()

        def sender(*args):
            self.sent.append(args)
            done.set()

        d = self.drainer(sender=sender)
        d.start()
        self.assertTrue(done.wait(5))
        d.stop()
        self.assertTrue(d._thread is None or not d._thread.is_alive())

    def test_loop_survives_unexpected_exception(self):
        self.put("a")
        done = threading.Event()
        calls = []

        def sender(*args):
            calls.append(1)
            done.set()

        d = self.drainer(sender=sender)
        real = self.spool.oldest
        state = {"n": 0}

        def flaky(dest_id):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("transient")
            return real(dest_id)

        self.spool.oldest = flaky
        d._idle = 0.01
        d.start()
        self.assertTrue(done.wait(5))
        d.stop()

    def _delete_fails(self, failing):
        real = self.spool.delete

        def delete(rowid):
            if failing["on"]:
                raise RuntimeError("disk full")
            real(rowid)

        self.spool.delete = delete

    def test_failing_delete_never_resends_via_thread_loop(self):
        self.put("a")
        self.put("b")
        failing = {"on": True}
        self._delete_fails(failing)
        d = self.drainer()
        d._idle = 0.01
        d.start()
        deadline = threading.Event()
        deadline.wait(0.5)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(d.drained("slot1"), 1)
        failing["on"] = False
        for _ in range(500):
            if len(self.sent) == 2 and self.spool.depth("slot1") == 0:
                break
            deadline.wait(0.01)
        d.stop()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.spool.depth("slot1"), 0)
        self.assertEqual([k[0] for k in self.store.recorded], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
