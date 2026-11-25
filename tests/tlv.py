"""Minimal BER reader for the tests (the shipped code only encodes)."""


def read_tlv(data: bytes, pos: int = 0):
    """Minimal BER reader: (tag, value, next_pos)."""
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(data[pos : pos + n], "big")
        pos += n
    return tag, data[pos : pos + length], pos + length


def children(value: bytes):
    out, pos = [], 0
    while pos < len(value):
        tag, val, pos = read_tlv(value, pos)
        out.append((tag, val))
    return out
