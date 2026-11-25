import unittest

from alarm_bridge.ber import (
    encode_length,
    encode_integer,
    encode_octet_string,
    encode_oid,
    encode_sequence,
)


class TestEncodeLength(unittest.TestCase):
    def test_short_form(self):
        self.assertEqual(encode_length(5), b"\x05")
        self.assertEqual(encode_length(0), b"\x00")
        self.assertEqual(encode_length(127), b"\x7f")

    def test_long_form(self):
        self.assertEqual(encode_length(128), b"\x81\x80")
        self.assertEqual(encode_length(300), b"\x82\x01\x2c")


class TestEncodeInteger(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(encode_integer(0), b"\x02\x01\x00")

    def test_small_positive(self):
        self.assertEqual(encode_integer(1), b"\x02\x01\x01")

    def test_leading_bit_set_gets_padding_byte(self):
        # 0x80 alone would look negative in two's complement; must be padded.
        self.assertEqual(encode_integer(128), b"\x02\x02\x00\x80")

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            encode_integer(-1)

    def test_custom_tag_for_timeticks(self):
        encoded = encode_integer(100, tag=0x43)
        self.assertEqual(encoded[0], 0x43)


class TestEncodeOctetString(unittest.TestCase):
    def test_str_input(self):
        self.assertEqual(encode_octet_string("AB"), b"\x04\x02AB")

    def test_bytes_input(self):
        self.assertEqual(encode_octet_string(b"AB"), b"\x04\x02AB")

    def test_empty_string(self):
        self.assertEqual(encode_octet_string(""), b"\x04\x00")


class TestEncodeOid(unittest.TestCase):
    def test_appneta_enterprise_oid(self):
        # 1.3.6.1.4.1.19041 -> first byte = 1*40+3 = 43 = 0x2b, then
        # 6,1,4,1 as single bytes, then 19041 as a multi-byte VLQ:
        # 19041 = 0x4A61 = 0b100_1010_0110_0001 -> split into 7-bit groups:
        # 1001010 0110 0001 -> [0b1001010=74 with cont bit, 0b1100001=0xE1]
        # 19041 in base-128: 19041 = 1*128^2? No: 128^2=16384, 19041-16384=2657
        # 2657 // 128 = 20 rem 97 -> groups (top->bottom): [1, 20, 97]
        encoded = encode_oid("1.3.6.1.4.1.19041")
        self.assertEqual(encoded[0], 0x06)  # tag
        body = encoded[2:]
        self.assertEqual(body[0], 43)  # 1*40+3
        self.assertEqual(list(body[1:5]), [6, 1, 4, 1])
        # last three bytes encode 19041 with continuation bits on all but the last
        self.assertEqual(body[5], 0x81)   # 1 with continuation bit
        self.assertEqual(body[6], 0x94)   # 20 with continuation bit
        self.assertEqual(body[7], 0x61)   # 97, no continuation bit

    def test_simple_oid(self):
        # 1.3.6.1.2.1.1.3.0 (sysUpTime.0)
        encoded = encode_oid("1.3.6.1.2.1.1.3.0")
        self.assertEqual(encoded[0], 0x06)
        body = encoded[2:]
        self.assertEqual(list(body), [43, 6, 1, 2, 1, 1, 3, 0])


class TestEncodeSequence(unittest.TestCase):
    def test_wraps_content_with_tag_and_length(self):
        content = b"\x02\x01\x05"
        self.assertEqual(encode_sequence(content), b"\x30\x03\x02\x01\x05")

    def test_custom_tag(self):
        content = b"\x02\x01\x05"
        self.assertEqual(encode_sequence(content, tag=0xA7), b"\xa7\x03\x02\x01\x05")


if __name__ == "__main__":
    unittest.main()
