"""Builds and sends SNMPv2c trap PDUs (RFC 3416) using only the BER
primitives in ber.py -- no third-party SNMP library dependency."""

import contextlib
import errno
import ipaddress
import os
import socket

from alarm_bridge import usm
from alarm_bridge.ber import encode_integer, encode_octet_string, encode_oid, encode_sequence

SYSUPTIME_OID = "1.3.6.1.2.1.1.3.0"
SNMP_TRAP_OID_OID = "1.3.6.1.6.3.1.1.4.1.0"
SNMP_TRAP_ADDRESS_OID = "1.3.6.1.6.3.18.1.3.0"

_SNMPV2C_VERSION = 1
_SNMPV3_VERSION = 3
_USM_SECURITY_MODEL = 3
_MSG_MAX_SIZE = 65507
_MSG_FLAGS_AUTH_PRIV = b"\x03"  # authPriv, not reportable (a trap has no response)
_TRAP_PDU_TAG = 0xA7
_TIMETICKS_TAG = 0x43
_IPADDRESS_TAG = 0x40

# After a send, wait this long for an ICMP port-unreachable to surface on the
# connected socket. Every successful send pays up to this much latency.
REFUSAL_WAIT_SECONDS = 0.05


class DestinationRefused(OSError):
    """The destination host answered the trap with ICMP port-unreachable."""


class UnsendableTrap(ValueError):
    """This trap can never be sent (too big for one datagram, or not encodable).

    A property of the alarm, not of the destination: callers must not mark the destination down.
    The message never carries alarm text.
    """


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


def _build_trap_pdu(request_id: int, sysuptime_ticks: int, trap_oid: str, varbinds) -> bytes:
    varbind_entries = [
        _encode_varbind(SYSUPTIME_OID, sysuptime_ticks, "timeticks"),
        _encode_varbind(SNMP_TRAP_OID_OID, trap_oid, "oid"),
    ]
    for oid, value, value_type in varbinds:
        varbind_entries.append(_encode_varbind(oid, value, value_type))
    varbind_list = encode_sequence(b"".join(varbind_entries))

    pdu_body = (
        encode_integer(request_id)
        + encode_integer(0)  # error-status
        + encode_integer(0)  # error-index
        + varbind_list
    )
    return encode_sequence(pdu_body, tag=_TRAP_PDU_TAG)


def build_snmpv2_trap(community: str, sysuptime_ticks: int, trap_oid: str, varbinds) -> bytes:
    pdu = _build_trap_pdu(1, sysuptime_ticks, trap_oid, varbinds)
    message_body = (
        encode_integer(_SNMPV2C_VERSION)
        + encode_octet_string(community)
        + pdu
    )
    return encode_sequence(message_body)


def build_snmpv3_trap(
    creds: usm.V3Credentials,
    boots: int,
    eng_time: int,
    msg_id: int,
    salt: bytes,
    sysuptime_ticks: int,
    trap_oid: str,
    varbinds,
) -> bytes:
    """SNMPv3 authPriv trap (RFC 3412/3414/3826). The sender is the authoritative engine
    for a trap, so msgAuthoritativeEngineID is our own engine id."""
    scoped_pdu = encode_sequence(
        encode_octet_string(creds.engine_id)  # contextEngineID
        + encode_octet_string(b"")  # contextName
        + _build_trap_pdu(msg_id, sysuptime_ticks, trap_oid, varbinds)
    )
    encrypted = usm.encrypt(creds, boots, eng_time, salt, scoped_pdu)

    def message(auth_params: bytes) -> bytes:
        security_parameters = encode_sequence(
            encode_octet_string(creds.engine_id)
            + encode_integer(boots)
            + encode_integer(eng_time)
            + encode_octet_string(creds.user)
            + encode_octet_string(auth_params)
            + encode_octet_string(salt)
        )
        return encode_sequence(
            encode_integer(_SNMPV3_VERSION)
            + encode_sequence(
                encode_integer(msg_id)
                + encode_integer(_MSG_MAX_SIZE)
                + encode_octet_string(_MSG_FLAGS_AUTH_PRIV)
                + encode_integer(_USM_SECURITY_MODEL)
            )
            + encode_octet_string(security_parameters)
            + encode_octet_string(encrypted)
        )

    placeholder = message(b"\x00" * usm.auth_params_length(creds.auth_protocol))
    return message(usm.authenticate(creds, placeholder))


def send_trap(
    host: str, port: int, community: str, trap_oid: str, varbinds, sysuptime_ticks: int = 0, v3=None
) -> None:
    try:
        if v3 is not None:
            # msgID doubles as the trap's request-id; 31 bits keeps it a positive INTEGER.
            msg_id = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF
            message = build_snmpv3_trap(
                v3, usm.engine_boots(), usm.engine_time(), msg_id, usm.new_salt(),
                sysuptime_ticks, trap_oid, varbinds,
            )
        else:
            message = build_snmpv2_trap(community, sysuptime_ticks, trap_oid, varbinds)
    except (ValueError, OverflowError) as exc:  # the message may quote alarm text: keep only the type
        raise UnsendableTrap(f"trap could not be encoded ({type(exc).__name__})") from exc
    if len(message) > _MSG_MAX_SIZE:
        raise UnsendableTrap(f"trap is {len(message)} bytes, over the {_MSG_MAX_SIZE}-byte UDP limit")
    # A connected UDP socket reports ICMP port-unreachable as ConnectionRefusedError,
    # but a send alone shows it only intermittently, so read back briefly. A
    # timeout means nothing objected (traps are unacknowledged), which is success.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(REFUSAL_WAIT_SECONDS)
        try:
            sock.connect((host, port))
            try:
                sock.send(message)
            except OSError as exc:
                # Only send() means this host can never send it (macOS caps UDP sends at 9216 bytes).
                # EMSGSIZE from recv() is a path MTU report (ICMP frag-needed): a destination failure.
                if exc.errno == errno.EMSGSIZE:
                    raise UnsendableTrap(f"trap is {len(message)} bytes, too large for this host's UDP send") from exc
                raise
            with contextlib.suppress(socket.timeout):  # nothing objected; a send() timeout is not success
                sock.recv(1)
        except ConnectionRefusedError as exc:
            raise DestinationRefused(f"{host}:{port} refused the trap (ICMP port unreachable)") from exc
