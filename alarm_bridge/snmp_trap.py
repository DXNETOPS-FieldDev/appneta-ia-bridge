"""Builds and sends SNMPv2c trap PDUs (RFC 3416) using only the BER
primitives in ber.py -- no third-party SNMP library dependency."""

import ipaddress
import socket

from alarm_bridge.ber import encode_integer, encode_octet_string, encode_oid, encode_sequence

SYSUPTIME_OID = "1.3.6.1.2.1.1.3.0"
SNMP_TRAP_OID_OID = "1.3.6.1.6.3.1.1.4.1.0"
SNMP_TRAP_ADDRESS_OID = "1.3.6.1.6.3.18.1.3.0"

_SNMPV2C_VERSION = 1
_TRAP_PDU_TAG = 0xA7
_TIMETICKS_TAG = 0x43
_IPADDRESS_TAG = 0x40


def _encode_varbind(oid: str, value, value_type: str) -> bytes:
    if value_type == "string":
        value_bytes = encode_octet_string(str(value))
    elif value_type == "integer":
        value_bytes = encode_integer(int(value))
    elif value_type == "oid":
        value_bytes = encode_oid(str(value))
    elif value_type == "timeticks":
        value_bytes = encode_integer(int(value), tag=_TIMETICKS_TAG)
    elif value_type == "ipaddress":
        try:
            packed = ipaddress.IPv4Address(str(value)).packed
        except ipaddress.AddressValueError as exc:
            raise ValueError(f"Invalid IPv4 address: {value!r}") from exc
        value_bytes = bytes([_IPADDRESS_TAG, len(packed)]) + packed
    else:
        raise ValueError(f"Unsupported varbind type: {value_type!r}")
    return encode_sequence(encode_oid(oid) + value_bytes)


def build_snmpv2_trap(community: str, sysuptime_ticks: int, trap_oid: str, varbinds) -> bytes:
    varbind_entries = [
        _encode_varbind(SYSUPTIME_OID, sysuptime_ticks, "timeticks"),
        _encode_varbind(SNMP_TRAP_OID_OID, trap_oid, "oid"),
    ]
    for oid, value, value_type in varbinds:
        varbind_entries.append(_encode_varbind(oid, value, value_type))
    varbind_list = encode_sequence(b"".join(varbind_entries))

    pdu_body = (
        encode_integer(1)  # request-id
        + encode_integer(0)  # error-status
        + encode_integer(0)  # error-index
        + varbind_list
    )
    pdu = encode_sequence(pdu_body, tag=_TRAP_PDU_TAG)

    message_body = (
        encode_integer(_SNMPV2C_VERSION)
        + encode_octet_string(community)
        + pdu
    )
    return encode_sequence(message_body)


def send_trap(host: str, port: int, community: str, trap_oid: str, varbinds, sysuptime_ticks: int = 0) -> None:
    message = build_snmpv2_trap(community, sysuptime_ticks, trap_oid, varbinds)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(message, (host, port))
