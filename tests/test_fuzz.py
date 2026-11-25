"""Property and fuzz tests for the BER encoders and the webhook mapping.

Deterministic by default. Override with FUZZ_SEED / FUZZ_ITERATIONS to explore;
the seed is in every assertion message so a failure can be replayed exactly.
"""

import copy
import json
import logging
import os
import pathlib
import random
import unittest

from alarm_bridge import ber
from alarm_bridge.mapping import AlarmMappingError, map_alarm_to_general_trap, map_alarm_to_trap
from alarm_bridge.snmp_trap import _encode_varbind
from tests.tlv import children, read_tlv

SEED = int(os.environ.get("FUZZ_SEED", "20240611"))
ITERATIONS = int(os.environ.get("FUZZ_ITERATIONS", "2000"))
FIXTURES = pathlib.Path(__file__).parent / "fixtures"

_NASTY_STRINGS = [
    "", "a" * 5000, "line1\r\nInjected: header", "\x00\x01\x02", "café 中文 \U0001f600",
    "\ud800", "-1", "1e400", "9" * 5000, " 42 ", "NaN", "RAISED", "MAJOR",
]


def _noise(rng, depth=0):
    kind = rng.randrange(9 if depth < 3 else 6)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.choice([0, 1, -1, 2**31, 2**63, -(2**70), rng.randrange(-10**9, 10**9)])
    if kind == 3:
        return rng.choice([0.0, 1.5, -2.5, float("inf"), float("nan"), 1e308])
    if kind == 4:
        return rng.choice(_NASTY_STRINGS)
    if kind == 5:
        return "".join(chr(rng.randrange(0x20, 0x2FFF)) for _ in range(rng.randrange(0, 40)))
    if kind in (6, 7):
        return [_noise(rng, depth + 1) for _ in range(rng.randrange(0, 4))]
    return {rng.choice([*_NASTY_STRINGS[:6], "category", "value"]): _noise(rng, depth + 1)
            for _ in range(rng.randrange(0, 4))}


def _corrupt(rng, base):
    """A realistic payload with a few fields dropped or replaced by noise."""
    payload = copy.deepcopy(base)
    keys = list(payload)
    for _ in range(rng.randrange(1, 5)):
        key = rng.choice([*keys, "tags", "state", "alarmSeverity", "orgId", "ruleId"])
        action = rng.randrange(3)
        if action == 0:
            payload.pop(key, None)
        elif action == 1:
            payload[key] = _noise(rng)
        else:
            payload[key] = rng.choice(_NASTY_STRINGS)
    return payload


def _random_payload(rng, bases):
    kind = rng.randrange(4)
    if kind == 0:
        return _noise(rng)
    if kind == 1:
        return {rng.choice(list(bases[0]) + _NASTY_STRINGS[:4]): _noise(rng) for _ in range(rng.randrange(0, 8))}
    return _corrupt(rng, rng.choice(bases))


def setUpModule():
    logging.disable(logging.CRITICAL)  # the mapper warns on every corrupted payload


def tearDownModule():
    logging.disable(logging.NOTSET)


def _decode_uint(value):
    return int.from_bytes(value, "big", signed=True)


class TestBerRoundTrip(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(SEED)
        self.msg = f"seed={SEED}"

    def test_integer_roundtrip(self):
        values = [0, 127, 128, 255, 256, 32767, 32768, 2**31 - 1]
        values += [self.rng.randrange(0, 2**31) for _ in range(ITERATIONS)]
        for n in values:
            encoded = ber.encode_integer(n)
            tag, value, end = read_tlv(encoded)
            self.assertEqual(tag, 0x02, f"{self.msg} n={n}")
            self.assertEqual(end, len(encoded), f"{self.msg} n={n}")
            self.assertEqual(_decode_uint(value), n, f"{self.msg} n={n}")

    def test_negative_integer_rejected(self):
        # Documented limitation: the encoder is unsigned-only.
        for n in (-1, -128, -129, -(2**31)):
            with self.assertRaises(ValueError, msg=f"{self.msg} n={n}"):
                ber.encode_integer(n)

    def test_octet_string_roundtrip(self):
        lengths = [0, 127, 128, 255, 256, 65535]
        lengths += [self.rng.randrange(0, 2000) for _ in range(min(ITERATIONS, 500))]
        for length in lengths:
            data = bytes(self.rng.randrange(256) for _ in range(length))
            encoded = ber.encode_octet_string(data)
            tag, value, end = read_tlv(encoded)
            self.assertEqual(tag, 0x04, f"{self.msg} len={length}")
            self.assertEqual(end, len(encoded), f"{self.msg} len={length}")
            self.assertEqual(value, data, f"{self.msg} len={length}")

    def test_sequence_nesting_roundtrip(self):
        def build(depth):
            """Returns (encoded, expected) where expected is a nested list/int/bytes."""
            if depth == 0 or self.rng.random() < 0.3:
                if self.rng.random() < 0.5:
                    n = self.rng.randrange(0, 2**31)
                    return ber.encode_integer(n), n
                data = bytes(self.rng.randrange(256) for _ in range(self.rng.randrange(0, 300)))
                return ber.encode_octet_string(data), data
            parts = [build(depth - 1) for _ in range(self.rng.randrange(0, 5))]
            return ber.encode_sequence(b"".join(e for e, _ in parts)), [x for _, x in parts]

        def decode(tag, value):
            if tag == 0x30:
                return [decode(t, v) for t, v in children(value)]
            return _decode_uint(value) if tag == 0x02 else value

        for _ in range(min(ITERATIONS, 300)):
            encoded, expected = build(4)
            tag, value, end = read_tlv(encoded)
            self.assertEqual(end, len(encoded), self.msg)
            self.assertEqual(decode(tag, value), expected, self.msg)


class TestRegressions(unittest.TestCase):
    def test_lone_surrogate_in_string_field_is_encodable(self):
        # A JSON payload may carry "\ud800"; strict UTF-8 encoding raised
        # UnicodeEncodeError, so the whole alarm was dropped with a 502 and retried forever.
        payload = {"state": "RAISED", "alarmSeverity": "MAJOR", "alarmId": "\ud800"}
        _, varbinds = map_alarm_to_trap(payload)
        for oid, value, vtype in varbinds:
            _encode_varbind(oid, value, vtype)
        self.assertEqual(ber.encode_octet_string("a\ud800b"), b"\x04\x03a?b")


class TestMappingFuzz(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(SEED)
        self.msg = f"seed={SEED}"
        self.bases = [json.loads((FIXTURES / n).read_text()) for n in ("alarm_raised.json", "alarm_cleared.json")]

    def _run(self, payload, offset):
        """Returns [(trap_oid, varbinds)] for each mapper that accepted the payload."""
        results = []
        for call in (lambda p: map_alarm_to_trap(p, offset), map_alarm_to_general_trap):
            try:
                results.append(call(copy.deepcopy(payload)))
            except AlarmMappingError:
                pass
            except Exception as exc:  # any exception here is the property under test
                self.fail(f"{self.msg} {type(exc).__name__}: {exc} payload={repr(payload)[:200]}")
        return results

    def test_mapping_never_raises_unexpected(self):
        for _ in range(ITERATIONS):
            payload = _random_payload(self.rng, self.bases)
            self._run(payload, self.rng.choice([None, 0, 2199023255552, 5]))

    def test_mapping_output_is_encodable(self):
        accepted = 0
        for _ in range(ITERATIONS):
            payload = _random_payload(self.rng, self.bases)
            for _trap_oid, varbinds in self._run(payload, self.rng.choice([None, 2199023255552])):
                accepted += 1
                for oid, value, vtype in varbinds:
                    try:
                        _encode_varbind(oid, value, vtype)
                    except Exception as exc:  # any exception here is the property under test
                        self.fail(f"{self.msg} {type(exc).__name__}: {exc} oid={oid} "
                                  f"payload={repr(payload)[:200]}")
        self.assertGreater(accepted, 0, f"{self.msg}: fuzz never produced a mappable payload")


if __name__ == "__main__":
    unittest.main()
