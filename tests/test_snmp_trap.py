import socket
import threading
import unittest

from alarm_bridge.snmp_trap import build_snmpv2_trap, send_trap
from alarm_bridge.ber import encode_octet_string, encode_oid


class TestBuildSnmpv2Trap(unittest.TestCase):
    def test_message_contains_community_string(self):
        message = build_snmpv2_trap("public", 0, "1.3.6.1.4.1.19041.2.1.103.0.90", [])
        self.assertIn(encode_octet_string("public"), message)

    def test_message_contains_trap_oid(self):
        trap_oid = "1.3.6.1.4.1.19041.2.1.103.0.90"
        message = build_snmpv2_trap("public", 0, trap_oid, [])
        self.assertIn(encode_oid(trap_oid), message)

    def test_message_contains_custom_varbind_oid_and_value(self):
        varbinds = [("1.3.6.1.4.1.19041.2.1.5.10.1", "abc-123", "string")]
        message = build_snmpv2_trap("public", 0, "1.3.6.1.4.1.19041.2.1.103.0.90", varbinds)
        self.assertIn(encode_oid("1.3.6.1.4.1.19041.2.1.5.10.1"), message)
        self.assertIn(encode_octet_string("abc-123"), message)

    def test_pdu_type_tag_is_snmpv2_trap(self):
        message = build_snmpv2_trap("public", 0, "1.3.6.1.4.1.19041.2.1.103.0.90", [])
        # SNMPv2-Trap-PDU context tag 0xA7 must appear somewhere in the message
        self.assertIn(0xA7, message)

    def test_outer_sequence_length_matches_actual_content(self):
        message = build_snmpv2_trap("public", 0, "1.3.6.1.4.1.19041.2.1.103.0.90", [])
        self.assertEqual(message[0], 0x30)  # outer SEQUENCE tag
        # short-form length (message body is small) must equal remaining bytes
        declared_length = message[1]
        self.assertLess(declared_length, 0x80)
        self.assertEqual(len(message) - 2, declared_length)


class TestSendTrap(unittest.TestCase):
    def test_sends_udp_packet_to_configured_destination(self):
        received = {}
        ready = threading.Event()

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(2)
        port = sock.getsockname()[1]

        def listener():
            ready.set()
            try:
                data, _addr = sock.recvfrom(65535)
                received["data"] = data
            except Exception as e:
                received["error"] = str(e)

        thread = threading.Thread(target=listener, daemon=True)
        thread.start()
        ready.wait(timeout=2)

        send_trap("127.0.0.1", port, "public", "1.3.6.1.4.1.19041.2.1.103.0.90", [])
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "listener thread did not finish in time")
        sock.close()

        self.assertIn("data", received)
        self.assertIn(encode_octet_string("public"), received["data"])


if __name__ == "__main__":
    unittest.main()
