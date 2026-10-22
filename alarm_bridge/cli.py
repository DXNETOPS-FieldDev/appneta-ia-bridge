"""appneta-ia-bridge entrypoint: load config from the environment and run the
webhook listener until stopped."""

import argparse
import logging
import sys

from alarm_bridge.config import ConfigError, TrapDestination, load_config, parse_source_address
from alarm_bridge.server import run_server
from alarm_bridge.snmp_trap import send_trap


def _parse_trap_destination(value: str) -> TrapDestination:
    parts = value.split(":")
    if len(parts) not in (3, 4, 5):
        raise argparse.ArgumentTypeError(
            f"--trap-destination must be HOST:PORT:COMMUNITY[:SCHEME[:SOURCE_ADDRESS]], got {value!r}"
        )
    host, raw_port, community = parts[0], parts[1], parts[2]
    scheme = parts[3] if len(parts) >= 4 else "vna"
    if scheme not in ("vna", "general"):
        raise argparse.ArgumentTypeError(f"scheme must be 'vna' or 'general', got {scheme!r}")
    try:
        port = int(raw_port)
    except ValueError:
        raise argparse.ArgumentTypeError(f"port must be an integer, got {raw_port!r}") from None
    try:
        source_address = parse_source_address(parts[4]) if len(parts) == 5 else ""
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid SOURCE_ADDRESS {parts[4]!r}: {exc}"
        ) from None
    return TrapDestination(
        host=host, port=port, community=community, scheme=scheme, source_address=source_address
    )


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="appneta-ia-bridge")
    parser.add_argument(
        "--trap-destination",
        action="append",
        type=_parse_trap_destination,
        dest="trap_destinations",
        help=(
            "HOST:PORT:COMMUNITY[:SCHEME[:SOURCE_ADDRESS]], repeatable. SOURCE_ADDRESS is "
            "an IPv4 address or 'none'; omitted means no snmpTrapAddress.0 claim. "
            "Overrides TRAP_DESTINATION_* env vars entirely (not merged) if given at all."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = load_config(trap_destinations=args.trap_destinations)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    run_server(config, send_trap)
    return 0
