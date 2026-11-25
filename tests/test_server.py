import errno
import json
import logging
import os
import random
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from typing import ClassVar

from alarm_bridge import mapping, snmp_trap
from alarm_bridge.config import Config, TrapDestination
from alarm_bridge.dedup import DedupStore, KeyedLocks
from alarm_bridge.obs import Stats
from alarm_bridge.server import make_handler, run_server
from alarm_bridge.snmp_trap import SNMP_TRAP_ADDRESS_OID, UnsendableTrap, send_trap
from alarm_bridge.spool import ErrorLog, SpoolError, SpoolStore


TOKEN = "test-token"


class TestServer(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_sender(host, port, community, trap_oid, varbinds):
            self.calls.append((host, port, community, trap_oid, varbinds))

        self.fake_sender = fake_sender
        self.config = Config(
            listen_port=0,
            auth_type="BEARER",
            auth_username="",
            auth_password="",
            auth_token=TOKEN,
            trap_destinations=[
                TrapDestination(host="127.0.0.1", port=162, community="public", scheme="vna"),
            ],
            v3_id_offset=None,
        )
        self._start(self.config, fake_sender)

    def _start(self, config, sender):
        handler = make_handler(config, sender)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)

    def _post(self, body: bytes, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/",
            data=body,
            headers=headers if headers is not None else {"Authorization": f"Bearer {TOKEN}"},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=2)
            return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_raised_alarm_returns_200_and_sends_trap(self):
        payload = json.dumps({"state": "RAISED", "alarmId": "abc", "orgId": "1", "alarmSeverity": "MAJOR"}).encode()
        status = self._post(payload)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "127.0.0.1")
        self.assertEqual(self.calls[0][1], 162)

    def test_malformed_json_returns_400_and_sends_no_trap(self):
        status = self._post(b"not json")
        self.assertEqual(status, 400)
        self.assertEqual(len(self.calls), 0)

    def test_unknown_alarm_state_returns_400(self):
        payload = json.dumps({"state": "WEIRD"}).encode()
        status = self._post(payload)
        self.assertEqual(status, 400)
        self.assertEqual(len(self.calls), 0)

    def _post_raw_content_length(self, value: str) -> int:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        conn.putrequest("POST", "/")
        conn.putheader("Content-Length", value)
        conn.endheaders()
        status = conn.getresponse().status
        conn.close()
        return status

    def test_non_numeric_content_length_returns_400(self):
        self.assertEqual(self._post_raw_content_length("abc"), 400)
        self.assertEqual(len(self.calls), 0)

    def test_negative_content_length_returns_400(self):
        self.assertEqual(self._post_raw_content_length("-5"), 400)

    def test_oversized_content_length_returns_413_without_reading_body(self):
        self.assertEqual(self._post_raw_content_length("999999999"), 413)
        self.assertEqual(len(self.calls), 0)

    def test_non_object_json_returns_400_not_a_dropped_connection(self):
        self.assertEqual(self._post(b"[1,2]"), 400)
        self.assertEqual(self._post(b"null"), 400)
        self.assertEqual(self._post(json.dumps({"state": "RAISED", "alarmSeverity": "MAJOR", "orgId": "abc"}).encode()), 400)
        self.assertEqual(len(self.calls), 0)

    def test_bad_auth_returns_401_and_sends_no_trap(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)

        auth_config = Config(
            listen_port=0, auth_type="BEARER", auth_username="", auth_password="",
            auth_token="secret",
            trap_destinations=[
                TrapDestination(host="127.0.0.1", port=162, community="public", scheme="vna"),
            ],
            v3_id_offset=None,
        )
        self._start(auth_config, lambda *a: self.calls.append(a))

        payload = json.dumps({"state": "RAISED", "alarmId": "abc"}).encode()
        status = self._post(payload, headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)
        self.assertEqual(len(self.calls), 0)

    def test_general_scheme_produces_general_trap_oid(self):
        from alarm_bridge.mapping import TRAP_OID_GENERAL_MAJOR_RAISED
        self.config.trap_destinations[0].scheme = "general"
        payload = json.dumps({
            "state": "RAISED", "alarmId": "abc", "orgId": "1", "alarmSeverity": "MAJOR",
        }).encode()
        status = self._post(payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[0][3], TRAP_OID_GENERAL_MAJOR_RAISED)

    def test_fans_out_to_multiple_destinations_with_correct_scheme_each(self):
        from alarm_bridge.mapping import TRAP_OID_ALARM_RAISED, TRAP_OID_GENERAL_MAJOR_RAISED
        self.httpd.shutdown()
        self.thread.join(timeout=2)

        config = Config(
            listen_port=0, auth_type="BEARER", auth_username="", auth_password="", auth_token=TOKEN,
            trap_destinations=[
                TrapDestination(host="spectrum", port=162, community="public", scheme="vna"),
                TrapDestination(host="smarts", port=162, community="public", scheme="general"),
            ],
            v3_id_offset=None,
        )
        self._start(config, self.fake_sender)

        payload = json.dumps({
            "state": "RAISED", "alarmId": "abc", "orgId": "1", "alarmSeverity": "MAJOR",
            "rule": "Testing Error - Connect",
        }).encode()
        status = self._post(payload)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][0], "spectrum")
        self.assertEqual(self.calls[0][3], TRAP_OID_ALARM_RAISED)
        self.assertEqual(self.calls[1][0], "smarts")
        self.assertEqual(self.calls[1][3], TRAP_OID_GENERAL_MAJOR_RAISED)

    def test_one_destination_send_failure_does_not_block_the_other(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)

        def flaky_sender(host, port, community, trap_oid, varbinds):
            if host == "unreachable":
                raise OSError("network unreachable")
            self.calls.append((host, port, community, trap_oid, varbinds))

        config = Config(
            listen_port=0, auth_type="BEARER", auth_username="", auth_password="", auth_token=TOKEN,
            trap_destinations=[
                TrapDestination(host="unreachable", port=162, community="public", scheme="general"),
                TrapDestination(host="127.0.0.1", port=162, community="public", scheme="general"),
            ],
            v3_id_offset=None,
        )
        self._start(config, flaky_sender)

        payload = json.dumps({
            "state": "RAISED", "alarmId": "abc", "orgId": "1", "alarmSeverity": "MAJOR",
        }).encode()
        status = self._post(payload)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "127.0.0.1")

    def _claim_config(self, destinations):
        return Config(
            listen_port=0,
            auth_type="BEARER",
            auth_username="",
            auth_password="",
            auth_token=TOKEN,
            trap_destinations=destinations,
            v3_id_offset=None,
        )

    def _raised(self):
        return json.dumps({
            "state": "RAISED", "alarmId": "abc", "orgId": "1", "alarmSeverity": "MAJOR",
        }).encode()

    def _restart(self, destinations):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self._start(self._claim_config(destinations), self.fake_sender)

    def test_every_send_failing_returns_502_so_the_sender_retries(self):
        def down(host, port, community, trap_oid, varbinds):
            raise OSError("network unreachable")

        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self._start(self._claim_config([
            TrapDestination("a", 162, "public", "general"),
            TrapDestination("b", 162, "public", "general"),
        ]), down)
        self.assertEqual(self._post(self._raised()), 502)

    def test_unmappable_payload_is_400_even_when_the_network_is_down(self):
        def down(host, port, community, trap_oid, varbinds):
            raise OSError("network unreachable")

        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self._start(self._claim_config([TrapDestination("a", 162, "public", "general")]), down)
        self.assertEqual(self._post(json.dumps({"state": "WEIRD"}).encode()), 400)

    def test_claim_is_last_varbind_with_ipaddress_type(self):
        self._restart([TrapDestination("127.0.0.1", 162, "public", "vna", "192.0.2.147")])
        self.assertEqual(self._post(self._raised()), 200)
        self.assertEqual(
            self.calls[0][4][-1], (SNMP_TRAP_ADDRESS_OID, "192.0.2.147", "ipaddress")
        )

    def test_opt_out_destination_sends_no_claim(self):
        self._restart([TrapDestination("127.0.0.1", 162, "public", "vna", "")])
        self.assertEqual(self._post(self._raised()), 200)
        self.assertNotIn(SNMP_TRAP_ADDRESS_OID, [vb[0] for vb in self.calls[0][4]])

    def test_two_destinations_same_scheme_get_their_own_claims(self):
        self._restart([
            TrapDestination("127.0.0.1", 162, "public", "vna", "192.0.2.147"),
            TrapDestination("127.0.0.2", 162, "public", "vna", "198.51.100.89"),
        ])
        self.assertEqual(self._post(self._raised()), 200)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][4][-1][1], "192.0.2.147")
        self.assertEqual(self.calls[1][4][-1][1], "198.51.100.89")
        self.assertEqual(len(self.calls[0][4]), len(self.calls[1][4]))
        for call in self.calls:
            claims = [vb for vb in call[4] if vb[0] == SNMP_TRAP_ADDRESS_OID]
            self.assertEqual(len(claims), 1)

    def test_mixed_claim_and_opt_out(self):
        self._restart([
            TrapDestination("127.0.0.1", 162, "public", "vna", "192.0.2.147"),
            TrapDestination("127.0.0.2", 162, "public", "vna", ""),
        ])
        self.assertEqual(self._post(self._raised()), 200)
        self.assertEqual(len(self.calls[0][4]), len(self.calls[1][4]) + 1)
        self.assertNotIn(SNMP_TRAP_ADDRESS_OID, [vb[0] for vb in self.calls[1][4]])

    def test_rejected_payload_still_400_with_claim(self):
        self._restart([TrapDestination("127.0.0.1", 162, "public", "vna", "192.0.2.147")])
        self.assertEqual(self._post(json.dumps({"state": "WEIRD"}).encode()), 400)
        self.assertEqual(len(self.calls), 0)

    # --- Hostile-but-authenticated payloads: every one must be answered, never a dropped connection ---

    def test_invalid_utf8_body_returns_400(self):
        self.assertEqual(self._post(b'{"a":"\xff"}'), 400)
        self.assertEqual(len(self.calls), 0)

    def test_overflowing_number_literals_return_400(self):
        # 1e999 parses to inf; int(inf) raises OverflowError. 5000 digits trips Python's int limit.
        for org in ("1e999", "9" * 5000):
            body = f'{{"state": "RAISED", "alarmSeverity": "MAJOR", "orgId": {org}}}'.encode()
            self.assertEqual(self._post(body), 400, org[:10])
        self.assertEqual(len(self.calls), 0)

    def test_unhashable_severity_returns_400_for_both_schemes(self):
        for scheme in ("vna", "general"):
            self._restart([TrapDestination("127.0.0.1", 162, "public", scheme)])
            body = json.dumps({"state": "RAISED", "alarmSeverity": ["MAJOR"], "orgId": "1"}).encode()
            self.assertEqual(self._post(body), 400, scheme)
        self.assertEqual(len(self.calls), 0)

    def test_unexpected_bug_in_mapping_returns_500_not_a_dropped_connection(self):
        with mock.patch.object(mapping, "map_alarm_to_trap", side_effect=RuntimeError("boom")):
            self.assertEqual(self._post(self._raised()), 500)
        self.assertEqual(len(self.calls), 0)


def _event(alarm_id="abc", state="RAISED", ts="2026-11-11T10:00:00Z", omit_ts=False, **extra):
    body = {"state": state, "alarmId": alarm_id, "orgId": "1", "alarmSeverity": "MAJOR"}
    if not omit_ts:
        body["raisedTime" if state == "RAISED" else "clearedTime"] = ts
    body.update(extra)
    return json.dumps(body).encode()


class TestServerDedup(unittest.TestCase):
    """De-duplication and outcome counting with a real in-memory store and Stats."""

    def setUp(self):
        self.calls = []
        self.calls_lock = threading.Lock()
        self.send_delay = 0.0
        self.down = False
        self.unsendable = False

        def fake_sender(host, port, community, trap_oid, varbinds):
            if self.send_delay:
                time.sleep(self.send_delay)
            if self.down:
                raise OSError("network unreachable")
            if self.unsendable:
                raise UnsendableTrap("trap exceeds the path MTU")
            with self.calls_lock:
                self.calls.append((host, port, community, trap_oid, varbinds))

        self.config = Config(
            listen_port=0, auth_type="BEARER", auth_username="", auth_password="",
            auth_token=TOKEN,
            trap_destinations=[TrapDestination(host="127.0.0.1", port=162, community="public", scheme="vna")],
            v3_id_offset=None,
        )
        self.store = DedupStore("", 96, 100000)
        self.stats = Stats(["127.0.0.1:162"], lambda: self.store.mode)
        self.locks = KeyedLocks()
        handler = make_handler(self.config, fake_sender, store=self.store, stats=self.stats, locks=self.locks)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()
        self.store.close()

    def _post(self, body, timeout=5):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/", data=body,
            headers={"Authorization": f"Bearer {TOKEN}"}, method="POST",
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout).status
        except urllib.error.HTTPError as exc:
            return exc.code

    def _count(self, name):
        return self.stats._counts[name]

    def test_concurrent_same_event_sends_once(self):
        self.send_delay = 0.2
        barrier = threading.Barrier(8)
        statuses = []

        def worker():
            barrier.wait()
            statuses.append(self._post(_event()))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(statuses, [200] * 8)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self._count("duplicates"), 7)
        self.assertEqual(self._count("delivered"), 1)
        self.assertEqual(self._count("received"), 8)

    def test_repeat_after_success_is_duplicate(self):
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self._count("duplicates"), 1)

    def test_all_fail_records_nothing_and_retry_sends(self):
        self.down = True
        self.assertEqual(self._post(_event()), 502)
        self.assertEqual(self._count("send_failed"), 1)
        self.down = False
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self._count("duplicates"), 0)
        self.assertEqual(self._count("delivered"), 1)

    def test_incident_replay_shape(self):
        bodies = [_event(alarm_id=f"alarm-{i}") for i in range(23)] * 33
        random.Random(1113).shuffle(bodies)
        with ThreadPoolExecutor(max_workers=16) as pool:
            statuses = list(pool.map(self._post, bodies))
        self.assertEqual(set(statuses), {200})
        self.assertEqual(len(self.calls), 23)
        self.assertEqual(self._count("duplicates"), 23 * 32)

    def test_raised_then_cleared_both_forwarded(self):
        self.assertEqual(self._post(_event(state="RAISED")), 200)
        self.assertEqual(self._post(_event(state="CLEARED", ts="2026-11-11T10:05:00Z")), 200)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self._count("duplicates"), 0)

    def test_undedupable_payloads_still_processed(self):
        bodies = [
            json.dumps({"state": "RAISED", "alarmId": 12345, "raisedTime": "t",
                        "orgId": "1", "alarmSeverity": "MAJOR"}).encode(),
            _event(omit_ts=True),
            _event(alarm_id="x" * 257),
        ]
        for body in bodies:
            self.assertEqual(self._post(body), 200)
            self.assertEqual(self._post(body), 200)  # not dedupable, so sent again
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(self._count("undedupable"), 6)
        self.assertEqual(self._post(b"[1,2]"), 400)
        self.assertEqual(self._count("rejected"), 1)

    def test_bad_json_and_unmappable_count_rejected(self):
        self.assertEqual(self._post(b"not json"), 400)
        self.assertEqual(self._post(_event(alarmSeverity="WEIRD")), 400)
        self.assertEqual(self._count("rejected"), 2)
        self.assertEqual(len(self.calls), 0)

    def test_summary_names_the_first_reject_reason(self):
        self.assertEqual(self._post(_event(alarmSeverity="WEIRD")), 400)
        self.assertEqual(self._post(b"not json"), 400)
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            self.stats.flush()
        [line] = [r.getMessage() for r in cm.records if r.getMessage().startswith("event=summary")]
        self.assertIn("first_reject=", line)
        self.assertIn("WEIRD", line)  # the mapper's reason, not just a count

    def test_unsendable_400_names_that_reason_not_unmappable(self):
        self.unsendable = True
        self.assertEqual(self._post(_event()), 400)
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            self.stats.flush()
        [line] = [r.getMessage() for r in cm.records if r.getMessage().startswith("event=summary")]
        self.assertIn("first_reject='unsendable: trap exceeds the path MTU'", line)

    def test_summary_names_invalid_json_as_the_reason(self):
        self.assertEqual(self._post(b"not json"), 400)
        with self.assertLogs("alarm_bridge", level="INFO") as cm:
            self.stats.flush()
        [line] = [r.getMessage() for r in cm.records if r.getMessage().startswith("event=summary")]
        self.assertIn("first_reject='invalid_json'", line)

    def test_bad_token_counts_auth_failed(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/", data=_event(),
            headers={"Authorization": "Bearer wrong"}, method="POST",
        )
        for _ in range(2):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(ctx.exception.code, 401)
        self.assertEqual(self._count("auth_failed"), 2)
        self.assertEqual(self._count("received"), 0)
        self.assertEqual(len(self.calls), 0)

    def test_bad_and_oversized_content_length_count_rejected(self):
        import http.client
        for value, expected in (("abc", 400), ("-5", 400), ("999999999", 413)):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            conn.putrequest("POST", "/")
            conn.putheader("Content-Length", value)
            conn.putheader("Authorization", f"Bearer {TOKEN}")
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, expected)
            conn.close()
        self.assertEqual(self._count("rejected"), 3)

    def test_same_host_different_ports_tracked_separately_without_flapping(self):
        self.config.trap_destinations = [
            TrapDestination(host="127.0.0.1", port=162, community="public", scheme="vna"),
            TrapDestination(host="127.0.0.1", port=1162, community="public", scheme="vna"),
        ]
        real_sender_calls = self.calls

        def sender(host, port, community, trap_oid, varbinds):
            if port == 1162:
                raise OSError("refused")
            real_sender_calls.append(port)

        handler = make_handler(self.config, sender, store=None, stats=self.stats)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/", data=_event(),
            headers={"Authorization": f"Bearer {TOKEN}"}, method="POST",
        )
        with self.assertLogs("alarm_bridge", "INFO") as cm:
            for _ in range(3):
                self.assertEqual(urllib.request.urlopen(request, timeout=5).status, 200)
        state = [r.getMessage() for r in cm.records if "event=dest_state" in r.getMessage()]
        self.assertEqual(len(state), 1, state)  # one FAILING for :1162, never an OK flap
        self.assertIn("dest=127.0.0.1:1162 state=FAILING", state[0])
        self.assertEqual(self.stats._dest["127.0.0.1:162"]["ok"], 3)
        self.assertEqual(self.stats._dest["127.0.0.1:1162"]["failed"], 3)
        self.assertNotIn("127.0.0.1", self.stats._dest)

    def test_waiter_timeout_returns_502_without_send(self):
        # The holder may be a different key on the same stripe, so a 200 here could lose that
        # event. 502 makes AppNeta retry; a same-key retry then becomes a duplicate.
        key = ("abc", "RAISED", "2026-11-11T10:00:00Z")
        self.assertTrue(self.locks.acquire(key, 1))
        try:
            with mock.patch("alarm_bridge.server.LOCK_WAIT_SECONDS", 0.2):
                self.assertEqual(self._post(_event()), 502)
        finally:
            # Raises RuntimeError if the handler released the lock it never acquired.
            self.locks.release(key)
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(self._count("inflight_timeouts"), 1)
        # The handler must not have released the lock it never held: still free exactly once.
        self.assertTrue(self.locks.acquire(key, 0.1))
        self.locks.release(key)
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(len(self.calls), 1)

    def test_send_failure_logs_without_traceback(self):
        self.down = True
        with self.assertLogs("alarm_bridge", "DEBUG") as cm:
            self.assertEqual(self._post(_event()), 502)
        failures = [r for r in cm.records if "Failed to send trap" in r.getMessage()]
        self.assertEqual(len(failures), 1)
        self.assertIsNone(failures[0].exc_info)
        self.assertEqual(failures[0].levelno, logging.DEBUG)
        self.assertIn("OSError: network unreachable", failures[0].getMessage())
        # The visible signal is the one-time destination state change, not a per-send line.
        state = [r for r in cm.records if "event=dest_state" in r.getMessage()]
        self.assertEqual(len(state), 1)
        self.assertEqual(state[0].levelno, logging.WARNING)

    def test_per_request_lines_are_debug_only(self):
        # At INFO, an unmappable payload and one missing an integer field (ruleId) must not log
        # per request; the summary's rejected counter carries the signal.
        with self.assertNoLogs("alarm_bridge", "INFO"):
            self.assertEqual(self._post(_event(alarmSeverity="WEIRD")), 400)
            self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self._count("rejected"), 1)

    def test_access_lines_are_debug_only(self):
        with self.assertLogs("alarm_bridge", "DEBUG") as cm:
            self.assertEqual(self._post(_event()), 200)
        access = [r for r in cm.records if r.funcName == "log_message"]
        self.assertTrue(access)
        self.assertTrue(all(r.levelno == logging.DEBUG for r in access))


class FakeHealth:
    def __init__(self):
        self.up = {}
        self.down = []

    def is_up(self, dest_id):
        return self.up.get(dest_id, True)

    def mark_down(self, dest_id, reason):
        self.up[dest_id] = False
        self.down.append((dest_id, reason))


KEY = ("abc", "RAISED", "2026-11-11T10:00:00Z")


class TestServerSpool(unittest.TestCase):
    """Per-destination deliver-or-spool flow with a real SpoolStore, a real store and fake health."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.calls = []
        self.calls_lock = threading.Lock()
        self.failing = set()  # hosts whose send raises
        self.send_delay = 0.0
        self.health = FakeHealth()
        self.store = DedupStore("", 96, 100000)
        self.addCleanup(self.store.close)
        self.stats = Stats(["192.0.2.1:162", "192.0.2.2:162"], lambda: self.store.mode)
        self.evicted = []
        self.real_port = None  # when set, traps really go to this local UDP port (size limits apply)

    def _sender(self, host, port, community, trap_oid, varbinds):
        if self.send_delay:
            time.sleep(self.send_delay)
        if host in self.failing:
            raise OSError("network unreachable")
        if self.real_port is not None:
            send_trap("127.0.0.1", self.real_port, community, trap_oid, varbinds)
        with self.calls_lock:
            self.calls.append((host, port, community, trap_oid, list(varbinds)))

    def _on_evict(self, batch):
        self.evicted.append(batch)
        for key_json, dest_id in batch:
            self.store.record((*json.loads(key_json), dest_id))

    def _start(self, dests=2, max_bytes=64 * 1024**2, spool=None, destinations=None):
        destinations = destinations or [
            TrapDestination(host=f"192.0.2.{i}", port=162, community="public", scheme="vna", slot=i)
            for i in range(1, dests + 1)
        ]
        self.config = Config(
            listen_port=0, auth_type="BEARER", auth_username="", auth_password="",
            auth_token=TOKEN, trap_destinations=destinations, v3_id_offset=None,
        )
        if spool is None:
            spool = SpoolStore(self._tmp.name, max_bytes, 0, disk_usage=lambda _p: SimpleNamespace(free=10 * 1024**3),
                               on_evict=self._on_evict)
            self.addCleanup(spool.close)
        self.spool = spool
        handler = make_handler(self.config, self._sender, store=self.store, stats=self.stats,
                               spool=spool, health=self.health)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

    def _post(self, body, timeout=5):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/", data=body,
            headers={"Authorization": f"Bearer {TOKEN}"}, method="POST",
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout).status
        except urllib.error.HTTPError as exc:
            return exc.code

    def _hosts(self):
        return sorted(c[0] for c in self.calls)

    def _head(self, dest_id):
        row = self.spool.oldest(dest_id)
        assert row is not None, f"spool for {dest_id} is empty"
        return row

    def test_all_up_delivers_and_records_per_destination(self):
        self._start()
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self._hosts(), ["192.0.2.1", "192.0.2.2"])
        self.assertTrue(self.store.seen((*KEY, "slot1")))
        self.assertTrue(self.store.seen((*KEY, "slot2")))
        self.assertFalse(self.store.seen(KEY))  # the per-destination record replaces the 3-part one
        self.assertEqual(self.spool.depth("slot1") + self.spool.depth("slot2"), 0)
        self.assertEqual(self.stats._counts["delivered"], 1)

    def test_one_down_one_up_delivers_one_spools_other_200(self):
        self._start()
        self.health.up["slot2"] = False
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self._hosts(), ["192.0.2.1"])
        self.assertEqual(self.spool.depth("slot1"), 0)
        self.assertEqual(self.spool.depth("slot2"), 1)
        _rowid, key_json, payload = self._head("slot2")
        self.assertEqual(tuple(json.loads(key_json)), KEY)
        self.assertEqual(payload["alarmId"], "abc")

    def test_all_down_spools_and_returns_200_not_502(self):
        self._start()
        self.health.up.update(slot1=False, slot2=False)
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.spool.depth("slot1"), 1)
        self.assertEqual(self.spool.depth("slot2"), 1)

    def test_duplicate_while_down_stored_once(self):
        self._start(dests=1)
        self.health.up["slot1"] = False
        with ThreadPoolExecutor(max_workers=33) as pool:
            statuses = list(pool.map(self._post, [_event()] * 33))
        self.assertEqual(statuses, [200] * 33)
        self.assertEqual(self.spool.depth("slot1"), 1)
        self.assertEqual(self.calls, [])

    def test_concurrent_same_alarm_while_up_sends_once(self):
        self._start(dests=1)
        self.send_delay = 0.05
        with ThreadPoolExecutor(max_workers=33) as pool:
            statuses = list(pool.map(self._post, [_event()] * 33))
        self.assertEqual(statuses, [200] * 33)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.spool.depth("slot1"), 0)
        self.assertEqual(self.stats._counts["duplicates"], 32)

    def test_replay_after_delivery_not_resent(self):
        self._start()
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.stats._counts["duplicates"], 1)

    def test_repost_counts_as_duplicate_when_another_destination_cannot_map_it(self):
        from unittest import mock

        from alarm_bridge import mapping

        self._start()
        real = mapping.map_for_destination

        def one_unmappable(payload, dest, offset, cache=None):
            if dest.host == "192.0.2.2":
                raise mapping.AlarmMappingError("nope")
            return real(payload, dest, offset, cache) if cache is not None else real(payload, dest, offset)

        with mock.patch.object(mapping, "map_for_destination", one_unmappable):
            self.assertEqual(self._post(_event()), 200)
            self.assertEqual(self._post(_event()), 200)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.stats._counts["duplicates"], 1)

    def test_legacy_three_part_record_counts_for_all_destinations(self):
        self._start()
        self.store.record(KEY)
        self.health.up["slot2"] = False
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.spool.depth("slot2"), 0)
        self.assertEqual(self.stats._counts["duplicates"], 1)

    def test_send_exception_marks_down_and_spools_that_alarm(self):
        self._start()
        self.failing.add("192.0.2.1")
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self._hosts(), ["192.0.2.2"])
        self.assertEqual([d for d, _ in self.health.down], ["slot1"])
        self.assertIn("OSError: network unreachable", self.health.down[0][1])
        self.assertEqual(self.spool.depth("slot1"), 1)
        self.assertFalse(self.store.seen((*KEY, "slot1")))
        self.assertTrue(self.store.seen((*KEY, "slot2")))

    def test_backlog_forces_new_alarm_behind_queue_even_if_up(self):
        self._start()
        self.spool.enqueue(json.dumps(["older", "RAISED", "t"]), "slot1", {"alarmId": "older"}, "older", "RAISED")
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self._hosts(), ["192.0.2.2"])
        self.assertEqual(self.spool.depth("slot1"), 2)
        self.assertEqual(json.loads(self._head("slot1")[1])[0], "older")  # order preserved

    def test_undedupable_payload_spooled_once_under_unique_key(self):
        self._start()
        self.health.up["slot1"] = False
        body = _event(omit_ts=True)
        self.assertEqual(self._post(body), 200)
        self.assertEqual(self.spool.depth("slot1"), 1)
        _rowid, key_json, _payload = self._head("slot1")
        key = json.loads(key_json)
        self.assertEqual(key[0], "undedupable")
        self.assertEqual(len(key), 2)
        self.assertEqual(self._hosts(), ["192.0.2.2"])
        self.assertEqual(len(self.store._memory), 0)  # never recorded
        self.assertEqual(self._post(body), 200)  # not dedupable: a second copy is a new row
        self.assertEqual(self.spool.depth("slot1"), 2)
        keys = set()
        while (row := self.spool.oldest("slot1")) is not None:
            keys.add(row[1])
            self.spool.delete(row[0])
        self.assertEqual(len(keys), 2)
        self.assertEqual(self.stats._counts["undedupable"], 2)

    def test_unmappable_payload_still_400(self):
        self._start()
        self.health.up["slot1"] = False
        self.assertEqual(self._post(_event(alarmSeverity="WEIRD")), 400)
        self.assertEqual(self.spool.depth("slot1"), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.stats._counts["rejected"], 1)

    def test_spool_write_failure_returns_503_and_logs_spool_error(self):
        self._start()
        self.health.up["slot1"] = False
        self.spool.close()  # any further spool call raises SpoolError
        with mock.patch("alarm_bridge.spool._ERROR_LOG", ErrorLog()), \
                self.assertLogs("alarm_bridge", "ERROR") as cm:
            self.assertEqual(self._post(_event()), 503)
        self.assertTrue(any("event=spool_error" in line for line in cm.output), cm.output)
        self.assertFalse(self.store.seen((*KEY, "slot1")))

    def test_spool_error_on_one_destination_still_attempts_others(self):
        tmp_spool = SpoolStore(self._tmp.name, 64 * 1024**2, 0)
        self.addCleanup(tmp_spool.close)
        real_enqueue = tmp_spool.enqueue

        def enqueue(key_json, dest_id, payload, alarm_id, state):
            if dest_id == "slot1":
                raise SpoolError("disk full")
            return real_enqueue(key_json, dest_id, payload, alarm_id, state)

        tmp_spool.enqueue = enqueue
        self._start(spool=tmp_spool)
        self.health.up["slot1"] = False
        self.assertEqual(self._post(_event()), 503)
        self.assertEqual(self._hosts(), ["192.0.2.2"])
        self.assertTrue(self.store.seen((*KEY, "slot2")))
        # The retry redoes only the destination that failed.
        tmp_spool.enqueue = real_enqueue
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self._hosts(), ["192.0.2.2"])
        self.assertEqual(tmp_spool.depth("slot1"), 1)

    def _evict_alarm_abc(self):
        """Spool alarm abc's raise, then fill until it is evicted; return depth after eviction."""
        self._start(dests=1, max_bytes=512 * 1024)
        self.health.up["slot1"] = False
        self.assertEqual(self._post(_event()), 200)
        target = json.dumps(list(KEY))
        for i in range(5000):
            self.spool.enqueue(json.dumps([f"fill-{i}", "RAISED", "t"]), "slot1",
                               {"alarmId": f"fill-{i}", "pad": "x" * 2000}, f"fill-{i}", "RAISED")
            if any(k == target for batch in self.evicted for k, _ in batch):
                break
        else:
            self.fail("alarm abc was never evicted")
        self.assertTrue(self.spool.raise_evicted("slot1", "abc"))
        return self.spool.depth("slot1"), len(self.evicted)

    def test_evicted_alarm_replay_not_stored_again(self):
        depth, batches = self._evict_alarm_abc()
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self.spool.depth("slot1"), depth)
        self.assertEqual(len(self.evicted), batches)

    def test_evicted_raise_reposted_by_appneta_not_spooled_again_with_run_server_recorder(self):
        # Same as above, but with the on_evict callback run_server installs, not the test's own.
        from alarm_bridge import server
        recorder = server._evict_recorder(self.store)

        def on_evict(batch):
            self.evicted.append(batch)
            recorder(batch)

        self._on_evict = on_evict
        depth, batches = self._evict_alarm_abc()
        self.assertTrue(self.store.seen((*KEY, "slot1")))
        self.assertEqual(self._post(_event()), 200)  # AppNeta retries the same alarmId/state/ts
        self.assertEqual(self.spool.depth("slot1"), depth)
        self.assertEqual(len(self.evicted), batches)
        target = json.dumps(list(KEY))
        rows = []
        while (row := self.spool.oldest("slot1")) is not None:
            rows.append(row[1])
            self.spool.delete(row[0])
        self.assertNotIn(target, rows)
        self.assertEqual(self.calls, [])

    def test_clear_after_evicted_raise_is_dropped_not_spooled(self):
        depth, batches = self._evict_alarm_abc()
        self.assertEqual(self._post(_event(state="CLEARED", ts="2026-11-11T10:05:00Z")), 200)
        self.assertEqual(self.spool.depth("slot1"), depth)
        self.assertEqual(len(self.evicted), batches)
        self.assertEqual(self.calls, [])

    def test_clear_with_delivered_raise_is_spooled_normally(self):
        self._start(dests=1)
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(len(self.calls), 1)
        self.health.up["slot1"] = False
        self.assertEqual(self._post(_event(state="CLEARED", ts="2026-11-11T10:05:00Z")), 200)
        self.assertEqual(self.spool.depth("slot1"), 1)
        _rowid, key_json, _payload = self._head("slot1")
        self.assertEqual(json.loads(key_json), ["abc", "CLEARED", "2026-11-11T10:05:00Z"])

    def test_per_request_lines_are_not_logged(self):
        self._start()
        self.health.up["slot2"] = False
        with self.assertNoLogs("alarm_bridge", "INFO"):
            self.assertEqual(self._post(_event()), 200)
            self.assertEqual(self._post(_event()), 200)
            self.assertEqual(self._post(_event(alarm_id="def")), 200)

    def test_request_path_neither_counts_the_spool_nor_repeats_the_legacy_lookup(self):
        # Hot path, once per webhook: a backlog check must not scan the destination's rows, and the
        # destination-independent 3-part (pre-spooling) record is looked up once, not once per destination.
        self._start(dests=3)
        depth_calls, legacy_lookups = [], []
        real_depth, real_seen = self.spool.depth, self.store.seen

        def counting_depth(dest_id):
            depth_calls.append(dest_id)
            return real_depth(dest_id)

        def counting_seen(key):
            if len(key) == 3:
                legacy_lookups.append(key)
            return real_seen(key)

        self.spool.depth = counting_depth
        self.store.seen = counting_seen
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(depth_calls, [])
        self.assertEqual(len(legacy_lookups), 1)

    def test_backlog_read_failure_still_sends_to_up_destination(self):
        self._start()

        def broken_has_backlog(dest_id):
            raise SpoolError("database is locked")

        self.spool.has_backlog = broken_has_backlog
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self._hosts(), ["192.0.2.1", "192.0.2.2"])
        self.assertTrue(self.store.seen((*KEY, "slot1")))
        self.assertTrue(self.store.seen((*KEY, "slot2")))

    def test_raise_evicted_read_failure_still_sends_clear_to_up_destination(self):
        self._start(dests=1)

        def broken_raise_evicted(dest_id, alarm_id):
            raise SpoolError("disk I/O error")

        self.spool.raise_evicted = broken_raise_evicted
        self.assertEqual(self._post(_event(state="CLEARED", ts="2026-11-11T10:05:00Z")), 200)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.store.seen(("abc", "CLEARED", "2026-11-11T10:05:00Z", "slot1")))

    def test_two_slots_same_host_port_are_independent(self):
        destinations = [
            TrapDestination(host="192.0.2.1", port=162, community="public", scheme="vna", slot=1),
            TrapDestination(host="192.0.2.1", port=162, community="public", scheme="vna", slot=2),
        ]
        self._start(destinations=destinations)
        self.health.up["slot1"] = False
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(len(self.calls), 1)  # slot2 delivered, slot1 spooled
        self.assertEqual(self.spool.depth("slot1"), 1)
        self.assertEqual(self.spool.depth("slot2"), 0)
        self.assertFalse(self.store.seen((*KEY, "slot1")))
        self.assertTrue(self.store.seen((*KEY, "slot2")))
        # A record for one slot does not suppress the other.
        other = ("def", "RAISED", "2026-11-11T10:00:00Z")
        self.store.record((*other, "slot1"))
        self.assertEqual(self._post(_event(alarm_id="def")), 200)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.spool.depth("slot1"), 1)  # slot1 skipped "def": no new row
        self.assertTrue(self.store.seen((*other, "slot2")))

    def test_enqueue_failure_returns_503_logs_once_and_keeps_existing_rows(self):
        self._start(dests=1)
        self.health.up["slot1"] = False
        self.spool.enqueue(json.dumps(["older", "RAISED", "t"]), "slot1", {"alarmId": "older"}, "older", "RAISED")
        real_conn = self.spool._conn
        assert real_conn is not None

        class InsertFails:
            """Proxy for the spool's connection: every INSERT fails as a full disk would."""

            def execute(self, sql, *args):
                if sql.lstrip().upper().startswith("INSERT"):
                    raise sqlite3.OperationalError("database or disk is full")
                return real_conn.execute(sql, *args)

            def close(self):
                real_conn.close()

        with mock.patch.object(self.spool, "_conn", InsertFails()), \
                mock.patch("alarm_bridge.spool._ERROR_LOG", ErrorLog()), \
                self.assertLogs("alarm_bridge", "ERROR") as cm:
            self.assertEqual(self._post(_event()), 503)
            self.assertEqual(self._post(_event(alarm_id="def")), 503)
        errors = [line for line in cm.output if "event=spool_error" in line]
        self.assertEqual(len(errors), 1, cm.output)  # rate limited
        self.assertEqual(self.spool.depth("slot1"), 1)
        self.assertEqual(json.loads(self._head("slot1")[1])[0], "older")
        self.assertEqual(self.calls, [])

    def _listen(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        self.real_port = listener.getsockname()[1]

    def test_oversized_alarm_is_400_destination_stays_up_no_flap(self):
        self._listen()
        self._start()
        big = _event(description="x" * 100_000)
        with self.assertLogs("alarm_bridge", "WARNING") as cm:
            self.assertEqual(self._post(big), 400)
        self.assertEqual(self.health.down, [])
        self.assertEqual(self.spool.depth("slot1") + self.spool.depth("slot2"), 0)
        self.assertTrue(self.store.seen((*KEY, "slot1")))  # owned: a retry is not processed again
        lines = [m for m in cm.output if "event=dropped_unsendable" in m]
        self.assertEqual([("dest=slot1" in m, "dest=slot2" in m) for m in lines], [(True, False), (False, True)])
        self.assertNotIn("xxxx", "\n".join(cm.output))
        self.assertEqual(self._post(big), 200)  # the retry is a duplicate, not another drop
        self.assertEqual(self.spool.depth("slot1") + self.spool.depth("slot2"), 0)
        self.assertEqual(self._post(_event(alarm_id="small")), 200)  # later alarms still deliver live
        self.assertEqual(self._hosts(), ["192.0.2.1", "192.0.2.2"])
        self.assertEqual(self.health.down, [])

    def test_emsgsize_from_recv_marks_down_and_spools(self):
        # PMTU frag-needed surfaces as EMSGSIZE on recv(): the path lost the trap, not the alarm's fault.
        self._listen()
        self._start(dests=1)

        class PmtuSocket:
            def __init__(self, *_a):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def settimeout(self, _t):
                pass

            def connect(self, _addr):
                pass

            def send(self, message):
                return len(message)

            def recv(self, _n):
                raise OSError(errno.EMSGSIZE, "Message too long")

        fake = SimpleNamespace(socket=PmtuSocket, AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM,
                               timeout=socket.timeout)
        with mock.patch.object(snmp_trap, "socket", fake):
            status = self._post(_event())
        self.assertEqual(status, 200)
        self.assertEqual([d for d, _r in self.health.down], ["slot1"])
        self.assertEqual(self.spool.depth("slot1"), 1)
        self.assertFalse(self.store.seen((*KEY, "slot1")))

    def test_oversized_alarm_without_spool_is_400_not_502(self):
        self._listen()
        self._start()
        handler = make_handler(self.config, self._sender, store=self.store, stats=self.stats)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]
        with self.assertLogs("alarm_bridge", "WARNING") as cm:
            self.assertEqual(self._post(_event(description="x" * 100_000)), 400)
        self.assertTrue(any("event=dropped_unsendable" in m for m in cm.output), cm.output)
        self.assertEqual(self.stats._counts["send_failed"], 0)

    def test_live_send_rechecks_dedup_after_backlog_check(self):
        # A drain that sends and records the alarm between the dedup check and the backlog check
        # (the drainer records before it deletes, so an empty backlog means the record is already there).
        self._start(dests=1)
        real_has_backlog = self.spool.has_backlog

        def has_backlog(dest_id):
            self.store.record((*KEY, dest_id))  # the drainer delivered it just now
            return real_has_backlog(dest_id)

        self.spool.has_backlog = has_backlog
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.spool.depth("slot1"), 0)
        self.assertEqual(self.stats._counts["duplicates"], 1)

    def test_repost_of_spooled_alarm_counts_as_duplicate(self):
        self._start(dests=1)
        self.health.up["slot1"] = False
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self._post(_event()), 200)
        self.assertEqual(self.spool.depth("slot1"), 1)
        self.assertEqual(self.stats._counts["duplicates"], 1)
        self.assertEqual(self.stats._counts["delivered"], 0)

    def test_spool_none_keeps_502_behaviour(self):
        self._start()  # builds the shared config; the no-spool handler below reuses it
        self.failing.update({"192.0.2.1", "192.0.2.2"})
        handler = make_handler(self.config, self._sender, store=self.store, stats=self.stats)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]
        self.assertEqual(self._post(_event()), 502)
        self.assertFalse(self.store.seen(KEY))
        self.failing.clear()
        self.assertEqual(self._post(_event()), 200)
        self.assertTrue(self.store.seen(KEY))  # the old path keeps its 3-part record
        self.assertEqual(self.spool.depth("slot1"), 0)


class TestRunServerBinding(unittest.TestCase):
    """run_server must bind the configured LISTEN_HOST, not a hard-coded address."""

    def _bound_address(self, **overrides):
        config = Config(
            listen_port=8080, auth_type="BEARER", auth_username="", auth_password="",
            auth_token="t", trap_destinations=[], v3_id_offset=None, **overrides,
        )
        with mock.patch("alarm_bridge.server.ThreadingHTTPServer") as server_cls:
            server_cls.return_value.serve_forever.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                run_server(config, lambda *a: None)
        return server_cls.call_args.args[0]

    def test_binds_configured_listen_host(self):
        self.assertEqual(self._bound_address(listen_host="127.0.0.1"), ("127.0.0.1", 8080))

    def test_binds_loopback_by_default(self):
        self.assertEqual(self._bound_address(), ("127.0.0.1", 8080))

    def test_run_server_wires_store_and_stats_and_cleans_up(self):
        dest = TrapDestination("127.0.0.1", 162, "public", "general")
        config = Config(
            listen_port=8080, auth_type="BEARER", auth_username="", auth_password="",
            auth_token="t", trap_destinations=[dest], v3_id_offset=None,
            dedup_ttl_hours=5, dedup_max_entries=500,
        )
        with mock.patch("alarm_bridge.server.ThreadingHTTPServer") as server_cls, \
                mock.patch("alarm_bridge.server.DedupStore") as store_cls, \
                mock.patch("alarm_bridge.server.Stats") as stats_cls:
            store_cls.return_value.mode = "memory"
            server_cls.return_value.serve_forever.side_effect = KeyboardInterrupt
            with self.assertLogs("alarm_bridge", "WARNING") as cm, self.assertRaises(KeyboardInterrupt):
                run_server(config, lambda *a: None)
        store_cls.assert_called_once_with("", 5, 500)
        self.assertEqual(stats_cls.call_args.args[0], ["127.0.0.1:162"])
        self.assertEqual(stats_cls.call_args.args[1](), "memory")
        stats = stats_cls.return_value
        stats.check_destinations.assert_called_once_with([dest])
        stats.start.assert_called_once()
        stats.stop.assert_any_call(timeout=0)  # signalled first, then joined
        stats.flush.assert_called_once()
        store_cls.return_value.close.assert_called_once()
        self.assertTrue(any("event=dedup_store mode=memory persistent=no" in line for line in cm.output))


def _spool_config(state_dir, dests=2, **overrides):
    destinations = [
        TrapDestination(host="127.0.0.1", port=1 + i, community="public", scheme="general", slot=i)
        for i in range(1, dests + 1)
    ]
    return Config(
        listen_port=0, auth_type="BEARER", auth_username="", auth_password="",
        auth_token=TOKEN, trap_destinations=destinations, v3_id_offset=None,
        dedup_state_dir=state_dir, spool_max_mb=16, spool_free_reserve_mb=0, **overrides,
    )


class FakeServer:
    """Stands in for ThreadingHTTPServer: serve_forever blocks until shutdown() is called."""

    instances: ClassVar[list] = []

    def __init__(self, address, handler, on_serve=None):
        self.address, self.handler = address, handler
        self._done = threading.Event()
        self.closed = False
        self.on_serve = on_serve
        FakeServer.instances.append(self)

    def serve_forever(self):
        if self.on_serve is not None:
            self.on_serve(self)
        if not self._done.wait(5):
            raise AssertionError("serve_forever was never shut down")

    def shutdown(self):
        self._done.set()

    def server_close(self):
        self.closed = True


class TestRunServerWiring(unittest.TestCase):
    """run_server: startup refusal, spool wiring, startup log and SIGTERM teardown."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        FakeServer.instances = []

    def _run(self, config, on_serve=None, sender=lambda *a: None):
        def make(address, handler):
            return FakeServer(address, handler, on_serve or (lambda s: s.shutdown()))

        with mock.patch("alarm_bridge.server.ThreadingHTTPServer", side_effect=make):
            return run_server(config, sender)

    def test_run_server_refuses_to_start_when_state_dir_unwritable(self):
        missing = os.path.join(self._tmp.name, "missing")
        with mock.patch("alarm_bridge.server.DedupStore") as store_cls, \
                self.assertLogs("alarm_bridge", "ERROR") as cm:
            error = self._run(_spool_config(missing))
        assert isinstance(error, str), error
        self.assertIn(missing, error)
        self.assertIn("10001", error)
        self.assertEqual(FakeServer.instances, [])  # never bound
        store_cls.assert_not_called()
        self.assertTrue(any(missing in line and "10001" in line for line in cm.output), cm.output)

    def test_run_server_starts_with_corrupt_spool_quarantined(self):
        with open(os.path.join(self._tmp.name, "spool.sqlite3"), "wb") as fh:
            fh.write(b"this is not a sqlite database" * 200)
        with self.assertLogs("alarm_bridge", "ERROR") as cm:
            error = self._run(_spool_config(self._tmp.name))
        self.assertIsNone(error)
        self.assertEqual(len(FakeServer.instances), 1)  # it served
        self.assertIsNotNone(FakeServer.instances[0].handler)
        asides = [n for n in os.listdir(self._tmp.name) if n.startswith("spool.sqlite3.corrupt-")]
        self.assertEqual(len(asides), 1)
        self.assertTrue(any("event=spool_error" in m and asides[0] in m for m in cm.output), cm.output)

    def test_run_server_refuses_to_start_when_spool_cannot_open(self):
        with mock.patch("alarm_bridge.spool.os.replace", side_effect=PermissionError("read-only")):
            with open(os.path.join(self._tmp.name, "spool.sqlite3"), "wb") as fh:
                fh.write(b"this is not a sqlite database" * 200)
            with self.assertLogs("alarm_bridge", "ERROR"):
                error = self._run(_spool_config(self._tmp.name))
        assert isinstance(error, str), error
        self.assertIn(self._tmp.name, error)
        self.assertEqual(FakeServer.instances, [])

    def test_run_server_without_state_dir_warns_and_keeps_502(self):
        config = _spool_config("")
        with mock.patch("alarm_bridge.server.make_handler", wraps=make_handler) as mh, \
                self.assertLogs("alarm_bridge", "WARNING") as cm:
            self.assertIsNone(self._run(config, sender=mock.Mock(side_effect=OSError("down"))))
        self.assertIsNone(mh.call_args.kwargs.get("spool"))
        self.assertIsNone(mh.call_args.kwargs.get("health"))
        self.assertTrue(any("event=spool mode=off" in line for line in cm.output), cm.output)
        # The handler it built still answers 502 when every send fails.
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeServer.instances[0].handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/", data=_event(),
            headers={"Authorization": f"Bearer {TOKEN}"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 502)

    def test_run_server_with_state_dir_wires_spool_health_and_drainer(self):
        config = _spool_config(self._tmp.name)
        with mock.patch("alarm_bridge.server.make_handler", wraps=make_handler) as mh, \
                mock.patch("alarm_bridge.server.Stats", wraps=Stats) as stats_cls, \
                mock.patch("alarm_bridge.server.DestinationHealth") as health_cls, \
                mock.patch("alarm_bridge.server.Drainer") as drainer_cls, \
                mock.patch.object(SpoolStore, "drop_orphans", autospec=True, return_value=0) as drop:
            self.assertIsNone(self._run(config))
        kwargs = mh.call_args.kwargs
        self.assertIsInstance(kwargs["spool"], SpoolStore)
        self.assertIs(kwargs["health"], health_cls.return_value)
        drop.assert_called_once_with(kwargs["spool"], {"slot1", "slot2"})
        h_args = health_cls.call_args.args
        self.assertEqual(h_args[:4], (config.trap_destinations, 10, 2, 15 * 60))
        d_args = drainer_cls.call_args.args
        self.assertEqual(d_args[:3], (config, kwargs["spool"], health_cls.return_value))
        self.assertEqual(d_args[5], 20)
        for obj in (health_cls.return_value, drainer_cls.return_value):
            obj.start.assert_called_once()
            obj.stop.assert_any_call(timeout=0)  # signalled first, then joined
        self.assertIsNotNone(stats_cls.call_args.kwargs["spool_fn"])
        self.assertIsNotNone(stats_cls.call_args.kwargs["state_fn"])

    def test_spool_stats_fn_combines_spool_and_drainer_counters(self):
        from alarm_bridge import server, spool as spool_mod
        config = _spool_config(self._tmp.name)
        store = DedupStore("", 96, 1000)
        self.addCleanup(store.close)
        spooling = server._build_spooling(config, store, lambda *a: None)
        self.addCleanup(spooling.spool.close)
        spooling.spool.enqueue(json.dumps(list(KEY)), "slot1", {"a": 1}, "abc", "RAISED")
        with mock.patch.object(spooling.drainer, "drained", return_value=4):
            stats = spooling.stats_fn("slot1")
        self.assertEqual(stats["depth"], 1)
        self.assertEqual(stats["evicted"], 0)
        self.assertEqual(stats["drained"], 4)
        self.assertGreaterEqual(stats["oldest_age"], 0)
        before = spooling.stats_fn(None)
        self.assertGreater(before["bytes"], 0)
        spool_mod.report_spool_error(OSError("x"))
        self.assertEqual(spooling.stats_fn(None)["errors"], before["errors"] + 1)
        # DestinationHealth was handed the same late-bound function.
        self.assertIs(spooling.health._stats, spooling.stats_fn)

    def test_stats_observers_aggregate_destinations_sharing_host_port(self):
        from alarm_bridge import server
        dests = [TrapDestination("192.0.2.1", 162, "a", "vna", slot=1),
                 TrapDestination("192.0.2.1", 162, "b", "vna", slot=2),
                 TrapDestination("192.0.2.2", 162, "c", "vna", slot=3)]
        health = FakeHealth()
        health.up["slot2"] = False
        per = {"slot1": {"depth": 1, "oldest_age": 5, "evicted": 1, "drained": 2},
               "slot2": {"depth": 3, "oldest_age": 9, "evicted": 0, "drained": 1},
               "slot3": {"depth": 0, "oldest_age": 0, "evicted": 0, "drained": 0}}
        totals = {"bytes": 8192, "errors": 1}
        spool_fn, state_fn = server._stats_observers(dests, health, lambda d: totals if d is None else per[d])
        self.assertEqual(spool_fn("192.0.2.1:162"), {"depth": 4, "oldest_age": 9, "evicted": 1, "drained": 3})
        self.assertEqual(spool_fn(None), totals)
        self.assertFalse(state_fn("192.0.2.1:162"))
        self.assertTrue(state_fn("192.0.2.2:162"))

    def test_on_evict_records_per_destination_keys_and_skips_undedupable(self):
        from alarm_bridge import server
        store = DedupStore("", 96, 1000)
        self.addCleanup(store.close)
        server._evict_recorder(store)([
            (json.dumps(list(KEY)), "slot1"),
            (json.dumps(["undedupable", "f00"]), "slot2"),
        ])
        self.assertTrue(store.seen((*KEY, "slot1")))
        self.assertFalse(store.seen(("undedupable", "f00", "slot2")))

    def test_startup_logs_mount_status_and_free_space(self):
        from alarm_bridge import server
        usage = lambda _p: SimpleNamespace(free=300 * 1024**2)  # noqa: E731
        with self.assertLogs("alarm_bridge", "INFO") as cm:
            server._log_state_dir("/state", disk_usage=usage, ismount=lambda _p: True)
            server._log_state_dir("/state", disk_usage=usage, ismount=lambda _p: False)
        self.assertEqual(len(cm.records), 2)
        self.assertEqual(cm.records[0].getMessage(), "event=state_dir path=/state mount_point=yes free_mb=300")
        self.assertEqual(cm.records[0].levelno, logging.INFO)
        self.assertTrue(cm.records[1].getMessage().startswith(
            "event=state_dir path=/state mount_point=no free_mb=300 ("), cm.records[1].getMessage())
        self.assertEqual(cm.records[1].levelno, logging.WARNING)

    def test_startup_log_survives_disk_usage_failure(self):
        from alarm_bridge import server
        def boom(_p):
            raise OSError("gone")
        with self.assertLogs("alarm_bridge", "INFO") as cm:
            server._log_state_dir("/state", disk_usage=boom, ismount=lambda _p: True)
        self.assertEqual(cm.records[0].getMessage(), "event=state_dir path=/state mount_point=yes free_mb=unknown")

    def test_run_server_logs_state_dir_at_startup(self):
        with self.assertLogs("alarm_bridge", "INFO") as cm:
            self._run(_spool_config(self._tmp.name))
        self.assertTrue(any(f"event=state_dir path={self._tmp.name} mount_point=" in line and "free_mb=" in line
                            for line in cm.output), cm.output)

    def test_sigterm_stops_threads_and_closes_stores(self):
        config = _spool_config(self._tmp.name)
        previous = signal.getsignal(signal.SIGTERM)

        def on_serve(srv):
            os.kill(os.getpid(), signal.SIGTERM)

        with mock.patch("alarm_bridge.server.DedupStore") as store_cls, \
                mock.patch("alarm_bridge.server.SpoolStore") as spool_cls, \
                mock.patch("alarm_bridge.server.DestinationHealth") as health_cls, \
                mock.patch("alarm_bridge.server.Drainer") as drainer_cls, \
                mock.patch("alarm_bridge.server.Stats") as stats_cls:
            spool_cls.return_value.drop_orphans.return_value = 0
            self.assertIsNone(self._run(config, on_serve=on_serve))
        self.assertTrue(FakeServer.instances[0].closed)
        health_cls.return_value.stop.assert_any_call(timeout=0)  # signalled first, then joined
        drainer_cls.return_value.stop.assert_any_call(timeout=0)  # signalled first, then joined
        stats_cls.return_value.stop.assert_any_call(timeout=0)  # signalled first, then joined
        stats_cls.return_value.flush.assert_called_once()
        store_cls.return_value.close.assert_called_once()
        spool_cls.return_value.close.assert_called_once()
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)  # handler restored

    def test_teardown_bounded_when_threads_ignore_stop(self):
        """Docker kills at 10 s: a wedged drainer/prober must not push flush and close past that."""
        release = threading.Event()
        self.addCleanup(release.set)

        class Stubborn:
            """A worker whose thread ignores stop (blocked mid-send or in DNS); stop() joins it."""

            def __init__(self, default_timeout):
                self._default = default_timeout
                self._thread = threading.Thread(target=release.wait, args=(30,), daemon=True)

            def start(self):
                self._thread.start()

            def stop(self, timeout=None):
                self._thread.join(self._default if timeout is None else timeout)

        def on_serve(srv):
            os.kill(os.getpid(), signal.SIGTERM)

        config = _spool_config(self._tmp.name)
        with mock.patch("alarm_bridge.server.DedupStore") as store_cls, \
                mock.patch("alarm_bridge.server.SpoolStore") as spool_cls, \
                mock.patch("alarm_bridge.server.DestinationHealth", return_value=Stubborn(5)), \
                mock.patch("alarm_bridge.server.Drainer", return_value=Stubborn(10)), \
                mock.patch("alarm_bridge.server.Stats") as stats_cls:
            spool_cls.return_value.drop_orphans.return_value = 0
            start = time.monotonic()
            self.assertIsNone(self._run(config, on_serve=on_serve))
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 4.5)  # one shared ~3 s budget, not 10 + 5 serially
        stats_cls.return_value.flush.assert_called_once()
        store_cls.return_value.close.assert_called_once()
        spool_cls.return_value.close.assert_called_once()

    def test_listen_socket_closed_before_threads_are_joined(self):
        order = []

        def on_serve(srv):
            srv.server_close = lambda: order.append("server_close")
            srv.shutdown()

        with mock.patch("alarm_bridge.server.DestinationHealth") as health_cls, \
                mock.patch("alarm_bridge.server.Drainer") as drainer_cls:
            drainer_cls.return_value.stop.side_effect = lambda *a, **k: order.append("drainer.stop")
            health_cls.return_value.stop.side_effect = lambda *a, **k: order.append("health.stop")
            self._run(_spool_config(self._tmp.name), on_serve=on_serve)
        self.assertEqual(order[0], "server_close", order)

    def test_run_server_in_a_thread_skips_signal_handler(self):
        result = {}
        thread = threading.Thread(target=lambda: result.update(r=self._run(_spool_config(""))))
        thread.start()
        thread.join(5)
        self.assertIn("r", result)
        self.assertIsNone(result["r"])


class TestSigtermProcess(unittest.TestCase):
    """docker stop sends SIGTERM to the real process: it must flush the summary and exit 0 promptly."""

    def test_sigterm_flushes_summary_and_exits_zero(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {
            "PATH": os.environ.get("PATH", ""), "LISTEN_PORT": str(port), "AUTH_TYPE": "BEARER",
            "AUTH_TOKEN": TOKEN, "TRAP_DESTINATION_1_HOST": "127.0.0.1", "TRAP_DESTINATION_1_PORT": "9",
            "TRAP_DESTINATION_1_COMMUNITY": "public", "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none", "DEDUP_STATE_DIR": tmp.name,
            "SPOOL_FREE_RESERVE_MB": "0",
        }
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.Popen([sys.executable, "-m", "alarm_bridge"], cwd=repo, env=env,
                                stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        lines = []
        reader = threading.Thread(target=lambda: lines.extend(proc.stderr), daemon=True)  # type: ignore[arg-type]
        reader.start()
        deadline = time.monotonic() + 10
        while not any("listening on port" in line for line in lines):
            self.assertLess(time.monotonic(), deadline, "".join(lines))
            self.assertIsNone(proc.poll(), "".join(lines))
            time.sleep(0.05)
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/", data=_event(),
            headers={"Authorization": f"Bearer {TOKEN}"}, method="POST",
        )
        self.assertEqual(urllib.request.urlopen(request, timeout=5).status, 200)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=8), 0)
        reader.join(2)
        out = "".join(lines)
        self.assertIn("event=summary", out)
        self.assertIn("received=1", out)
        self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main()
