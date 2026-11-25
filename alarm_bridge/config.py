"""Environment-variable-driven configuration for appneta-ia-bridge."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field

from alarm_bridge.auth import SUPPORTED_AUTH_TYPES


class ConfigError(ValueError):
    """Raised when required configuration is missing or inconsistent."""


# An HTTP header field name is a token; this is the safe subset. No underscores: nginx
# drops them by default (underscores_in_headers off), so the header would never arrive.
_HEADER_NAME_RE = re.compile(r"[A-Za-z0-9-]{1,64}")

# A destination NAME becomes its dest_id: spool rows, state keys, log lines. Keep it a plain token.
_DEST_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _check_destination_ids(destinations: list[TrapDestination], missing: list[str]) -> None:
    """Assigns slots, validates NAMEs and rejects duplicate dest_ids. Errors go to `missing`."""
    seen: dict[str, int] = {}
    for slot, dest in enumerate(destinations, start=1):
        dest.slot = slot
        dest.name = dest.name.strip()
        if dest.name and (not _DEST_NAME_RE.fullmatch(dest.name) or dest.name in (".", "..")):
            missing.append(
                f"TRAP_DESTINATION_{slot}_NAME must be 1-64 characters of letters, digits, "
                f"'_', '.', '-' (not '.' or '..'), got {dest.name!r}"
            )
            continue
        if dest.dest_id in seen:
            missing.append(
                f"TRAP_DESTINATION_{slot}_NAME: destination id {dest.dest_id!r} is already used "
                f"by destination {seen[dest.dest_id]}"
            )
        else:
            seen[dest.dest_id] = slot


# v4->v3 ID offsets per AppNeta platform host (v3_id = v4_id - offset), keyed by
# the server label in its hostname (https://<label>.pm.appneta.com). Hosts not
# listed here require APPNETA_V3_ID_OFFSET to be set explicitly.
NIS_SERVER_V3_ID_OFFSETS: dict[str, int] = {
    "app-01": 2199023255552,
    "app-02": 12094627905536,
    "app-03": 4398046511104,
    "app-05": 16492674416640,
    "app-06": 18691697672192,
    "app-07": 3298534883328,
    "app-08": 19791209299968,
    "app-09": 17592186044416,
    "app-10": 23089744183296,
    "app-11": 7696581394432,
    "app-12": 10995116277760,
    "app-13": 24189255811072,
    "app-14": 9895604649984,
    "app-16": 15393162788864,
    "app-18": 25288767438848,
    "app-21": 21990232555520,
    "demo": 1099511627776,
    "marriott": 26388279066624,
}


def parse_source_address(raw: str) -> str:
    """'none' (any case, surrounding whitespace) -> "" (opt-out); otherwise the
    normalized dotted IPv4. Raises ValueError for anything else, including empty."""
    value = raw.strip()
    if value.lower() == "none":
        return ""
    return str(ipaddress.IPv4Address(value))


@dataclass
class TrapDestination:
    host: str
    port: int
    community: str
    scheme: str = "vna"
    source_address: str = ""  # "" = opt-out; otherwise a validated dotted IPv4
    slot: int = 0  # 1-based list position, assigned by load_config
    name: str = ""  # optional operator label (TRAP_DESTINATION_<N>_NAME)
    probe_tcp_port: int = 0  # 0 = no TCP probe

    @property
    def dest_id(self) -> str:
        return self.name or f"slot{self.slot}"


@dataclass
class Config:
    listen_port: int
    auth_type: str
    auth_username: str
    auth_password: str
    auth_token: str
    trap_destinations: list[TrapDestination]
    v3_id_offset: int | None
    auth_header_name: str = ""  # only used when auth_type == "HEADER"
    listen_host: str = "127.0.0.1"  # loopback by default: only nginx (or an explicit LISTEN_HOST) may reach alarm_bridge
    dedup_state_dir: str = ""
    dedup_ttl_hours: int = 96
    dedup_max_entries: int = 100000
    log_level: str = "INFO"
    log_syslog_targets: list[tuple[str, int]] = field(default_factory=list)  # [] = syslog off
    log_syslog_host: str = ""  # first target, kept for callers that read one
    log_syslog_port: int = 0
    log_syslog_protocol: str = "udp"
    spool_max_mb: int = 256
    spool_free_reserve_mb: int = 128
    spool_drain_rate: int = 20
    probe_interval_seconds: int = 10
    up_after_passes: int = 2
    reminder_minutes: int = 15
    metrics_auth_token: str = ""  # read-only bearer token; setting it is what serves GET /metrics, empty means no endpoint

    @property
    def spool_enabled(self) -> bool:
        return bool(self.dedup_state_dir)


def _int_env(name: str, raw) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def read_log_level(env=None) -> str:
    """Validated uppercase LOG_LEVEL (default INFO); lets cli.main set up logging before load_config."""
    env = env if env is not None else os.environ
    level = (env.get("LOG_LEVEL") or "").strip().upper() or "INFO"
    if level not in _LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}, got {env.get('LOG_LEVEL')!r}")
    return level


def _bounded_int_env(
    env, name: str, default: int, minimum: int, missing: list[str], maximum: int | None = None
) -> int:
    """Optional integer setting >= minimum (and <= maximum if given); unset/empty -> default.
    Errors go to `missing`."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = _int_env(name, raw)
    except ConfigError as exc:
        missing.append(str(exc))
        return default
    if value < minimum:
        missing.append(f"{name} must be at least {minimum}, got {value}")
        return default
    if maximum is not None and value > maximum:
        missing.append(f"{name} must be at most {maximum}, got {value}")
        return default
    return value


def _parse_syslog_address(env, missing: list[str]) -> list[tuple[str, int]]:
    """LOG_SYSLOG_ADDRESS=host:port[,host:port...] (each split on the last colon); unset/empty
    -> syslog off. A bad entry is reported by itself (repr, so control characters are escaped)."""
    raw = (env.get("LOG_SYSLOG_ADDRESS") or "").strip()
    targets = []
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        host, sep, port_text = entry.rpartition(":")
        port = int(port_text) if port_text.isascii() and port_text.isdigit() else 0
        if host.startswith("[") and host.endswith("]"):  # IPv6 literal, e.g. [::1]:514
            host = host[1:-1]
        if not sep or not host or not 1 <= port <= 65535:
            missing.append(
                f"LOG_SYSLOG_ADDRESS entries must be host:port with port 1-65535, got {entry[:100]!r}"
            )
            return []
        targets.append((host, port))
    return targets


def _read_secret(env, value_key: str, file_key: str) -> str:
    """Reads a secret from `<KEY>_FILE` if set (takes precedence), else `<KEY>`.

    File-based secrets let a production deployment keep credentials in a
    restricted-permission file on disk -- mounted into the container the same
    way as the TLS key -- rather than as literal values in `docker run -e`,
    which Docker persists in plaintext in its own container config so it can
    restart the container automatically after a reboot.
    """
    file_path = env.get(file_key)
    if not file_path:
        return env.get(value_key, "")
    try:
        with open(file_path) as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Could not read {file_key}={file_path!r}: {exc}") from exc


def _parse_trap_destinations(env, missing: list[str]) -> list[TrapDestination]:
    """Parses TRAP_DESTINATION_<N>_{HOST,PORT,COMMUNITY[_FILE],SCHEME,SOURCE_ADDRESS,NAME,
    PROBE_TCP_PORT}, N=1,2,... Stops at the first N with no _HOST set. At least destination 1 is required."""
    destinations = []
    n = 1
    while True:
        host = env.get(f"TRAP_DESTINATION_{n}_HOST")
        if not host:
            break
        raw_scheme = env.get(f"TRAP_DESTINATION_{n}_SCHEME")
        if not raw_scheme:
            missing.append(
                f"TRAP_DESTINATION_{n}_SCHEME is required (must be 'vna' or 'general' -- "
                "there is no default, see README Step 3)"
            )
            scheme = ""
        else:
            scheme = raw_scheme.lower()
            if scheme not in ("vna", "general"):
                missing.append(
                    f"TRAP_DESTINATION_{n}_SCHEME must be 'vna' or 'general', got {scheme!r}"
                )
        raw_source = env.get(f"TRAP_DESTINATION_{n}_SOURCE_ADDRESS")
        source_address = ""
        try:
            if raw_source is None:
                raise ValueError("unset")
            source_address = parse_source_address(raw_source)
        except ValueError:
            missing.append(
                f"TRAP_DESTINATION_{n}_SOURCE_ADDRESS is required and must be a dotted IPv4 "
                f"address (the Spectrum model / Smarts element IP for this sender), or 'none' "
                f"to opt out, "
                + ("but it is not set" if raw_source is None else f"got {raw_source!r}")
            )
        community = _read_secret(
            env, f"TRAP_DESTINATION_{n}_COMMUNITY", f"TRAP_DESTINATION_{n}_COMMUNITY_FILE"
        )
        try:
            community.encode("utf-8")
        except UnicodeEncodeError:  # env bytes that are not UTF-8 arrive as lone surrogates
            missing.append(f"TRAP_DESTINATION_{n}_COMMUNITY must be valid UTF-8")
        if not community:
            missing.append(
                f"TRAP_DESTINATION_{n}_COMMUNITY or TRAP_DESTINATION_{n}_COMMUNITY_FILE is "
                "required (the SNMP community string; there is no default)"
            )
        probe_tcp_port = _bounded_int_env(
            env, f"TRAP_DESTINATION_{n}_PROBE_TCP_PORT", 0, 1, missing, maximum=65535
        )
        destinations.append(TrapDestination(
            host=host,
            port=_int_env(f"TRAP_DESTINATION_{n}_PORT", env.get(f"TRAP_DESTINATION_{n}_PORT", "162")),
            community=community,
            scheme=scheme,
            source_address=source_address,
            name=(env.get(f"TRAP_DESTINATION_{n}_NAME") or "").strip(),
            probe_tcp_port=probe_tcp_port,
        ))
        n += 1

    if not destinations:
        missing.append("TRAP_DESTINATION_1_HOST")
    return destinations


def load_config(env=None, trap_destinations=None) -> Config:
    """`trap_destinations`, if given, bypasses TRAP_DESTINATION_* env parsing entirely --
    used by the CLI's --trap-destination flag, which overrides env rather than merging."""
    env = env if env is not None else os.environ

    missing = []

    if trap_destinations is None:
        trap_destinations = _parse_trap_destinations(env, missing)

    _check_destination_ids(trap_destinations, missing)

    raw_auth_type = env.get("AUTH_TYPE")
    auth_type = (raw_auth_type or "").strip().upper()
    if not auth_type:
        missing.append(
            "AUTH_TYPE (no default -- this endpoint is reachable from the public "
            "internet; set one of " + ", ".join(SUPPORTED_AUTH_TYPES) + ")"
        )
    elif auth_type not in SUPPORTED_AUTH_TYPES:
        missing.append(
            f"AUTH_TYPE={raw_auth_type!r} is not supported -- use one of "
            + ", ".join(SUPPORTED_AUTH_TYPES)
            + " (there is no unauthenticated mode)"
        )
    auth_token = _read_secret(env, "AUTH_TOKEN", "AUTH_TOKEN_FILE")
    auth_password = _read_secret(env, "AUTH_PASSWORD", "AUTH_PASSWORD_FILE")
    if auth_type in ("BEARER", "HEADER") and not auth_token:
        missing.append(f"AUTH_TOKEN or AUTH_TOKEN_FILE (required when AUTH_TYPE={auth_type})")
    auth_header_name = env.get("AUTH_HEADER_NAME", "")
    if auth_type == "HEADER" and not _HEADER_NAME_RE.fullmatch(auth_header_name):
        missing.append(
            "AUTH_HEADER_NAME (required when AUTH_TYPE=HEADER; letters, digits and hyphens only, "
            "1-64 characters -- no underscores, nginx drops those by default)"
        )
    if auth_type == "BASIC" and not auth_password:
        missing.append("AUTH_PASSWORD or AUTH_PASSWORD_FILE (required when AUTH_TYPE=BASIC)")

    raw_v3_id_offset = env.get("APPNETA_V3_ID_OFFSET")
    nis_server = env.get("APPNETA_NIS_SERVER")
    if raw_v3_id_offset:
        v3_id_offset = _int_env("APPNETA_V3_ID_OFFSET", raw_v3_id_offset)
    elif nis_server and nis_server in NIS_SERVER_V3_ID_OFFSETS:
        v3_id_offset = NIS_SERVER_V3_ID_OFFSETS[nis_server]
    else:
        v3_id_offset = None

    needs_v3_id_offset = any(dest.scheme == "vna" for dest in trap_destinations)
    if needs_v3_id_offset and v3_id_offset is None:
        if nis_server:
            missing.append(
                f"APPNETA_NIS_SERVER={nis_server!r} has no known offset in this build's table "
                f"({', '.join(sorted(NIS_SERVER_V3_ID_OFFSETS))}) -- set APPNETA_V3_ID_OFFSET "
                "directly instead"
            )
        else:
            missing.append(
                "APPNETA_V3_ID_OFFSET or APPNETA_NIS_SERVER (required when "
                "TRAP_DESTINATION_*_SCHEME=vna -- the vna scheme identifies paths by 32-bit IDs) "
                "-- set APPNETA_NIS_SERVER to your AppNeta platform host's label (e.g. app-12, "
                "from https://app-12.pm.appneta.com) if it is listed in the table, or "
                "APPNETA_V3_ID_OFFSET directly otherwise"
            )

    dedup_ttl_hours = _bounded_int_env(env, "DEDUP_TTL_HOURS", 96, 1, missing)
    dedup_max_entries = _bounded_int_env(env, "DEDUP_MAX_ENTRIES", 100000, 100, missing)
    spool_max_mb = _bounded_int_env(env, "SPOOL_MAX_MB", 256, 16, missing)
    spool_free_reserve_mb = _bounded_int_env(env, "SPOOL_FREE_RESERVE_MB", 128, 0, missing)
    spool_drain_rate = _bounded_int_env(env, "SPOOL_DRAIN_RATE", 20, 1, missing, maximum=1000)
    probe_interval_seconds = _bounded_int_env(
        env, "DEST_PROBE_INTERVAL_SECONDS", 10, 2, missing, maximum=300)
    up_after_passes = _bounded_int_env(env, "DEST_UP_AFTER_PASSES", 2, 1, missing, maximum=10)
    reminder_minutes = _bounded_int_env(
        env, "DEST_DOWN_REMINDER_MINUTES", 15, 1, missing, maximum=1440)
    metrics_auth_token = _read_secret(env, "METRICS_AUTH_TOKEN", "METRICS_AUTH_TOKEN_FILE")
    if metrics_auth_token and metrics_auth_token == auth_token:
        raise ConfigError(
            "METRICS_AUTH_TOKEN is the same value as AUTH_TOKEN. The webhook token must not be reused for "
            "/metrics: generate a separate read-only token (openssl rand -hex 32) for METRICS_AUTH_TOKEN, "
            "or leave it unset to keep /metrics off."
        )
    try:
        log_level = read_log_level(env)
    except ConfigError as exc:
        missing.append(str(exc))
        log_level = "INFO"
    log_syslog_targets = _parse_syslog_address(env, missing)
    log_syslog_protocol = (env.get("LOG_SYSLOG_PROTOCOL") or "").strip().lower() or "udp"
    if log_syslog_protocol not in ("udp", "tcp"):
        missing.append(
            f"LOG_SYSLOG_PROTOCOL must be 'udp' or 'tcp', got {env.get('LOG_SYSLOG_PROTOCOL')!r}"
        )
        log_syslog_protocol = "udp"

    if missing:
        raise ConfigError("Missing required configuration: " + ", ".join(missing))

    listen_host = env.get("LISTEN_HOST", "127.0.0.1").strip()
    try:
        listen_host = str(ipaddress.IPv4Address(listen_host))
    except ValueError:
        raise ConfigError(f"LISTEN_HOST must be a dotted IPv4 address, got {listen_host!r}") from None

    return Config(
        listen_host=listen_host,
        listen_port=_int_env("LISTEN_PORT", env.get("LISTEN_PORT", "8080")),
        auth_type=auth_type,
        auth_username=env.get("AUTH_USERNAME", ""),
        auth_password=auth_password,
        auth_token=auth_token,
        trap_destinations=trap_destinations,
        v3_id_offset=v3_id_offset,
        auth_header_name=auth_header_name,
        dedup_state_dir=env.get("DEDUP_STATE_DIR", "").strip(),
        dedup_ttl_hours=dedup_ttl_hours,
        dedup_max_entries=dedup_max_entries,
        log_level=log_level,
        log_syslog_targets=log_syslog_targets,
        log_syslog_host=log_syslog_targets[0][0] if log_syslog_targets else "",
        log_syslog_port=log_syslog_targets[0][1] if log_syslog_targets else 0,
        log_syslog_protocol=log_syslog_protocol,
        spool_max_mb=spool_max_mb,
        spool_free_reserve_mb=spool_free_reserve_mb,
        spool_drain_rate=spool_drain_rate,
        metrics_auth_token=metrics_auth_token,
        probe_interval_seconds=probe_interval_seconds,
        up_after_passes=up_after_passes,
        reminder_minutes=reminder_minutes,
    )
