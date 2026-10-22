import socket
import threading
import unittest

from alarm_bridge.snmp_trap import SNMP_TRAP_ADDRESS_OID, _encode_varbind, build_snmpv2_trap, send_trap
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


class TestIpAddressVarbind(unittest.TestCase):
    def test_ipaddress_varbind_encodes_tag_0x40_with_four_octets(self):
        varbind = _encode_varbind("1.3.6.1.6.3.18.1.3.0", "192.0.2.147", "ipaddress")
        self.assertTrue(varbind.endswith(b"\x40\x04\xc0\x00\x02\x93"))

    def test_ipaddress_varbind_rejects_invalid(self):
        for bad in ("256.1.1.1", "1.2.3", "::1", "host", ""):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    _encode_varbind(SNMP_TRAP_ADDRESS_OID, bad, "ipaddress")

    def test_claim_is_exactly_one_extra_varbind(self):
        trap_oid = "1.3.6.1.4.1.19041.2.1.103.0.90"
        base = [("1.3.6.1.4.1.19041.2.1.5.10.1", "abc", "string")]
        claim = (SNMP_TRAP_ADDRESS_OID, "192.0.2.147", "ipaddress")
        without = build_snmpv2_trap("public", 0, trap_oid, base)
        with_claim = build_snmpv2_trap("public", 0, trap_oid, base + [claim])
        encoded = _encode_varbind(*claim)
        self.assertTrue(with_claim.endswith(encoded))
        # Both messages stay in short-form BER lengths, so no length-field growth.
        self.assertLess(with_claim[1], 0x80)
        self.assertEqual(len(with_claim) - len(without), len(encoded))


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
