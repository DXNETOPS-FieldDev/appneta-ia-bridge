import hmac
import os
import socket
import subprocess
import sys
import unittest
import warnings

from alarm_bridge import usm
from alarm_bridge.ber import encode_oid
from alarm_bridge.snmp_trap import build_snmpv3_trap, send_trap
from tests.tlv import children, read_tlv

ENGINE_ID = bytes.fromhex("80001f8880aabbccdd00112233")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRAP_OID = "1.3.6.1.4.1.19041.2.1.103.0.90"


def _creds(auth="SHA256"):
    return usm.V3Credentials("iabridge", ENGINE_ID, auth, "authpass-123", "privpass-456")


def _parse(message: bytes):
    _, body, _ = read_tlv(message)
    version, header, sec_params, scoped = children(body)
    _, sp_body, _ = read_tlv(sec_params[1])
    engine_id, boots, eng_time, user, auth, salt = children(sp_body)
    return {
        "version": int.from_bytes(version[1], "big"),
        "header": [v for _, v in children(header[1])],
        "engine_id": engine_id[1],
        "boots": int.from_bytes(boots[1], "big"),
        "time": int.from_bytes(eng_time[1], "big"),
        "user": user[1],
        "auth": auth[1],
        "salt": salt[1],
        "encrypted": scoped[1],
        "sec_params": sec_params[1],
    }


class TestKeyLocalization(unittest.TestCase):
    def test_rfc3414_a3_2_sha_vector(self):
        # RFC 3414 appendix A.3.2: password "maplesyrup", engineID 00..02
        engine = bytes.fromhex("000000000000000000000002")
        key = usm.localized_key("maplesyrup", engine, "SHA")
        self.assertEqual(key.hex(), "6695febc9288e36282235fc7151f128497b38f3f")

    def test_key_length_follows_hash(self):
        for proto, size in (("SHA", 20), ("SHA256", 32), ("SHA384", 48), ("SHA512", 64)):
            self.assertEqual(len(usm.localized_key("longenough", ENGINE_ID, proto)), size)

    def test_short_password_rejected(self):
        for pw in ("", "1234567"):
            with self.assertRaises(ValueError):
                usm.localized_key(pw, ENGINE_ID, "SHA256")
        self.assertEqual(len(usm.localized_key("12345678", ENGINE_ID, "SHA256")), 32)

    def test_key_depends_on_engine_id(self):
        a = usm.localized_key("longenough", ENGINE_ID, "SHA256")
        b = usm.localized_key("longenough", ENGINE_ID + b"\x01", "SHA256")
        self.assertNotEqual(a, b)


class TestEngineClock(unittest.TestCase):
    def test_boots_is_positive_31_bit(self):
        self.assertGreater(usm.engine_boots(), 0)
        self.assertLess(usm.engine_boots(), 2**31)

    def test_time_never_negative(self):
        self.assertGreaterEqual(usm.engine_time(), 0)


@unittest.skipUnless(usm.crypto_available(), "cryptography not installed")
class TestBuildSnmpv3Trap(unittest.TestCase):
    def _build(self, creds, boots=7, eng_time=42, salt=b"\x01" * 8):
        return build_snmpv3_trap(creds, boots, eng_time, 12345, salt, 100, TRAP_OID, [])

    def test_header_fields(self):
        parsed = _parse(self._build(_creds()))
        self.assertEqual(parsed["version"], 3)
        msg_id, _max_size, flags, model = parsed["header"]
        self.assertEqual(int.from_bytes(msg_id, "big"), 12345)
        self.assertEqual(flags, b"\x03")  # authPriv, reportable bit clear
        self.assertEqual(int.from_bytes(model, "big"), 3)
        self.assertEqual(parsed["engine_id"], ENGINE_ID)
        self.assertEqual((parsed["boots"], parsed["time"]), (7, 42))
        self.assertEqual(parsed["user"], b"iabridge")

    def test_auth_param_length_per_protocol(self):
        for proto in usm.AUTH_PROTOCOLS:
            parsed = _parse(self._build(_creds(proto)))
            self.assertEqual(len(parsed["auth"]), usm.auth_params_length(proto))

    def test_hmac_verifies_with_zeroed_auth_field(self):
        for proto, (hash_name, mac_len) in usm.AUTH_PROTOCOLS.items():
            creds = _creds(proto)
            message = self._build(creds)
            parsed = _parse(message)
            zeroed = message.replace(parsed["auth"], b"\x00" * mac_len, 1)
            key = usm.localized_key(creds.auth_password, ENGINE_ID, proto)
            expected = hmac.new(key, zeroed, hash_name).digest()[:mac_len]
            self.assertEqual(parsed["auth"], expected, proto)

    def test_tampered_message_fails_hmac(self):
        creds = _creds()
        message = bytearray(self._build(creds))
        parsed = _parse(bytes(message))
        message[-1] ^= 0xFF  # flip a ciphertext byte
        zeroed = bytes(message).replace(parsed["auth"], b"\x00" * 24, 1)
        key = usm.localized_key(creds.auth_password, ENGINE_ID, "SHA256")
        self.assertNotEqual(hmac.new(key, zeroed, "sha256").digest()[:24], parsed["auth"])

    def test_encrypt_emits_no_deprecation_warning(self):
        from cryptography.utils import CryptographyDeprecationWarning

        with warnings.catch_warnings():
            warnings.simplefilter("error", CryptographyDeprecationWarning)
            out = usm.encrypt(_creds(), 7, 42, b"\x01" * 8, b"x" * 20)
        self.assertEqual(len(out), 20)

    def test_v2c_send_does_not_import_cryptography(self):
        code = (
            f"import sys; sys.path.insert(0, {ROOT!r}); sys.modules['cryptography'] = None\n"
            "from alarm_bridge.snmp_trap import build_snmpv2_trap\n"
            "build_snmpv2_trap('public', 0, '1.3.6.1.4.1.19041.2.1.103.0.90', [])\n"
        )
        result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_scoped_pdu_decrypts_to_trap(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

        creds = _creds()
        parsed = _parse(self._build(creds))
        key = usm.localized_key(creds.priv_password, ENGINE_ID, "SHA256")[:16]
        iv = parsed["boots"].to_bytes(4, "big") + parsed["time"].to_bytes(4, "big") + parsed["salt"]
        decryptor = Cipher(algorithms.AES(key), usm._cfb(iv)).decryptor()
        scoped = decryptor.update(parsed["encrypted"]) + decryptor.finalize()

        tag, body, _ = read_tlv(scoped)
        self.assertEqual(tag, 0x30)
        context_engine, context_name, pdu = children(body)
        self.assertEqual(context_engine[1], ENGINE_ID)
        self.assertEqual(context_name[1], b"")
        self.assertEqual(pdu[0], 0xA7)  # SNMPv2-Trap-PDU
        self.assertIn(encode_oid(TRAP_OID), pdu[1])

    def test_wrong_priv_password_does_not_decrypt(self):
        creds = _creds()
        parsed = _parse(self._build(creds))
        wrong = usm.V3Credentials("iabridge", ENGINE_ID, "SHA256", "authpass-123", "not-the-password")
        again = usm.encrypt(wrong, parsed["boots"], parsed["time"], parsed["salt"], b"x" * 20)
        same = usm.encrypt(creds, parsed["boots"], parsed["time"], parsed["salt"], b"x" * 20)
        self.assertNotEqual(again, same)

    def test_salt_changes_ciphertext(self):
        a = _parse(self._build(_creds(), salt=b"\x01" * 8))["encrypted"]
        b = _parse(self._build(_creds(), salt=b"\x02" * 8))["encrypted"]
        self.assertNotEqual(a, b)

    def test_send_trap_v3_over_udp(self):
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(2)
        try:
            send_trap("127.0.0.1", receiver.getsockname()[1], "", TRAP_OID, [], v3=_creds())
            data, _ = receiver.recvfrom(65535)
        finally:
            receiver.close()
        parsed = _parse(data)
        self.assertEqual(parsed["version"], 3)
        self.assertEqual(parsed["user"], b"iabridge")
        self.assertGreater(parsed["boots"], 0)


if __name__ == "__main__":
    unittest.main()
