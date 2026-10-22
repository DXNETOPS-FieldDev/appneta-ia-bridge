import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from alarm_bridge.config import Config, TrapDestination
from alarm_bridge.server import make_handler
from alarm_bridge.snmp_trap import SNMP_TRAP_ADDRESS_OID


class TestServer(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_sender(host, port, community, trap_oid, varbinds):
            self.calls.append((host, port, community, trap_oid, varbinds))

        self.fake_sender = fake_sender
        self.config = Config(
            listen_port=0,
            auth_type="NONE",
            auth_username="",
            auth_password="",
            auth_token="",
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
            headers=headers or {},
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
            listen_port=0, auth_type="NONE", auth_username="", auth_password="", auth_token="",
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
            listen_port=0, auth_type="NONE", auth_username="", auth_password="", auth_token="",
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
            auth_type="NONE",
            auth_username="",
            auth_password="",
            auth_token="",
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


if __name__ == "__main__":
    unittest.main()
