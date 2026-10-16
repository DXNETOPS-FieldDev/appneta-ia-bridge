"""Minimal BER encoding primitives for hand-rolling an SNMPv2c trap PDU.

Only what appneta-ia-bridge needs: non-negative INTEGER, OCTET STRING, OBJECT
IDENTIFIER, and SEQUENCE. No decoding, no ASN.1 general-purpose support.
"""


def encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    encoded = []
    n = length
    while n:
        encoded.insert(0, n & 0xFF)
        n >>= 8
    return bytes([0x80 | len(encoded)]) + bytes(encoded)


def encode_integer(value: int, tag: int = 0x02) -> bytes:
    if value < 0:
        raise ValueError("encode_integer does not support negative values (not needed by appneta-ia-bridge)")
    if value == 0:
        body = b"\x00"
    else:
        body = b""
        n = value
        while n:
            body = bytes([n & 0xFF]) + body
            n >>= 8
        if body[0] & 0x80:
            body = b"\x00" + body
    return bytes([tag]) + encode_length(len(body)) + body


def encode_octet_string(value, tag: int = 0x04) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return bytes([tag]) + encode_length(len(value)) + value


def encode_oid(oid: str, tag: int = 0x06) -> bytes:
    parts = [int(p) for p in oid.strip(".").split(".")]
    if len(parts) < 2:
        raise ValueError(f"OID must have at least two arcs: {oid!r}")
    body = bytearray([parts[0] * 40 + parts[1]])
    for part in parts[2:]:
        if part == 0:
            body.append(0)
            continue
        chunk = []
        n = part
        while n:
            chunk.insert(0, n & 0x7F)
            n >>= 7
        for i in range(len(chunk) - 1):
            chunk[i] |= 0x80
        body.extend(chunk)
    return bytes([tag]) + encode_length(len(body)) + bytes(body)


def encode_sequence(content: bytes, tag: int = 0x30) -> bytes:
    return bytes([tag]) + encode_length(len(content)) + content
