import errno
import socket
import threading
import time
import unittest
from unittest import mock

from alarm_bridge.snmp_trap import (
    SNMP_TRAP_ADDRESS_OID,
    DestinationRefused,
    UnsendableTrap,
    _encode_varbind,
    build_snmpv2_trap,
    send_trap,
)
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
            with self.subTest(value=bad), self.assertRaises(ValueError):
                _encode_varbind(SNMP_TRAP_ADDRESS_OID, bad, "ipaddress")

    def test_claim_is_exactly_one_extra_varbind(self):
        trap_oid = "1.3.6.1.4.1.19041.2.1.103.0.90"
        base = [("1.3.6.1.4.1.19041.2.1.5.10.1", "abc", "string")]
        claim = (SNMP_TRAP_ADDRESS_OID, "192.0.2.147", "ipaddress")
        without = build_snmpv2_trap("public", 0, trap_oid, base)
        with_claim = build_snmpv2_trap("public", 0, trap_oid, [*base, claim])
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


class TestSendTrapRefusal(unittest.TestCase):
    OID = "1.3.6.1.4.1.19041.2.1.103.0.90"

    def test_send_trap_raises_destination_refused_on_closed_port(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(DestinationRefused):
            send_trap("127.0.0.1", port, "public", self.OID, [])

    def test_destination_refused_is_oserror(self):
        self.assertTrue(issubclass(DestinationRefused, OSError))

    def test_send_trap_succeeds_when_listener_present_and_stays_fast(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            start = time.monotonic()
            send_trap("127.0.0.1", sock.getsockname()[1], "public", self.OID, [])
            self.assertLess(time.monotonic() - start, 0.5)


class TestUnsendableTrap(unittest.TestCase):
    """A trap that can never be sent is the alarm's fault, not the destination's: not an OSError."""

    OID = "1.3.6.1.4.1.19041.2.1.103.0.90"

    def test_is_not_an_oserror(self):
        self.assertFalse(issubclass(UnsendableTrap, OSError))

    def test_oversized_trap_raises_before_any_send(self):
        big = [("1.3.6.1.4.1.19041.2.1.5.10.7", "x" * 100_000, "string")]
        with mock.patch("alarm_bridge.snmp_trap.socket.socket") as sock_cls, \
                self.assertRaises(UnsendableTrap) as cm:
            send_trap("127.0.0.1", 9, "public", self.OID, big)
        sock_cls.assert_not_called()
        self.assertIn("bytes", str(cm.exception))
        self.assertNotIn("xxxx", str(cm.exception))

    def test_emsgsize_from_the_socket_is_unsendable(self):
        with mock.patch("alarm_bridge.snmp_trap.socket.socket") as sock_cls:
            sock_cls.return_value.__enter__.return_value.send.side_effect = OSError(errno.EMSGSIZE, "Message too long")
            with self.assertRaises(UnsendableTrap):
                send_trap("127.0.0.1", 9, "public", self.OID, [])

    def test_emsgsize_from_recv_is_a_destination_failure_not_unsendable(self):
        # Linux PMTU discovery: ICMP frag-needed on the path sets EMSGSIZE on the connected socket and
        # recv() raises it. Path-specific, so it must stay an OSError (mark down, spool, retry).
        with mock.patch("alarm_bridge.snmp_trap.socket.socket") as sock_cls:
            sock_cls.return_value.__enter__.return_value.recv.side_effect = OSError(errno.EMSGSIZE, "Message too long")
            with self.assertRaises(OSError) as cm:
                send_trap("127.0.0.1", 9, "public", self.OID, [])
        self.assertNotIsInstance(cm.exception, UnsendableTrap)
        self.assertEqual(cm.exception.errno, errno.EMSGSIZE)

    def test_send_timeout_is_a_failure_not_a_delivery(self):
        # A timeout from send() (full socket buffer) means the datagram never left; only a timeout
        # from the read-back means "nothing objected". Swallowing it deleted the spooled alarm unsent.
        with mock.patch("alarm_bridge.snmp_trap.socket.socket") as sock_cls:
            sock_cls.return_value.__enter__.return_value.send.side_effect = socket.timeout("timed out")
            with self.assertRaises(OSError) as cm:
                send_trap("127.0.0.1", 9, "public", self.OID, [])
        self.assertNotIsInstance(cm.exception, UnsendableTrap)

    def test_recv_timeout_is_success(self):
        with mock.patch("alarm_bridge.snmp_trap.socket.socket") as sock_cls:
            sock_cls.return_value.__enter__.return_value.recv.side_effect = socket.timeout("timed out")
            send_trap("127.0.0.1", 9, "public", self.OID, [])

    def test_other_socket_oserror_is_not_unsendable(self):
        with mock.patch("alarm_bridge.snmp_trap.socket.socket") as sock_cls:
            sock_cls.return_value.__enter__.return_value.send.side_effect = OSError(errno.ENETUNREACH, "unreachable")
            with self.assertRaises(OSError) as cm:
                send_trap("127.0.0.1", 9, "public", self.OID, [])
        self.assertNotIsInstance(cm.exception, UnsendableTrap)

    def test_encode_error_is_unsendable_without_the_value(self):
        with self.assertRaises(UnsendableTrap) as cm:
            send_trap("127.0.0.1", 9, "public", self.OID, [("1.3.6.1.4.1.1", "SEKRIT", "nosuchtype")])
        self.assertNotIn("SEKRIT", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
