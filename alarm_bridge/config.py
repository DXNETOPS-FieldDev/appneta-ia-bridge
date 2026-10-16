"""Environment-variable-driven configuration for appneta-ia-bridge."""

import os
from dataclasses import dataclass


class ConfigError(ValueError):
    """Raised when required configuration is missing or inconsistent."""


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


@dataclass
class TrapDestination:
    host: str
    port: int
    community: str
    scheme: str = "vna"


@dataclass
class Config:
    listen_port: int
    auth_type: str
    auth_username: str
    auth_password: str
    auth_token: str
    trap_destinations: list[TrapDestination]
    v3_id_offset: int | None


def _int_env(name: str, raw) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


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
    except OSError as exc:
        raise ConfigError(f"Could not read {file_key}={file_path!r}: {exc}") from exc


def _parse_trap_destinations(env, missing: list[str]) -> list[TrapDestination]:
    """Parses TRAP_DESTINATION_<N>_{HOST,PORT,COMMUNITY,SCHEME}, N=1,2,... Stops
    at the first N with no _HOST set. At least destination 1 is required."""
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
        destinations.append(TrapDestination(
            host=host,
            port=_int_env(f"TRAP_DESTINATION_{n}_PORT", env.get(f"TRAP_DESTINATION_{n}_PORT", "162")),
            community=env.get(f"TRAP_DESTINATION_{n}_COMMUNITY", "public"),
            scheme=scheme,
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

    raw_auth_type = env.get("AUTH_TYPE")
    if not raw_auth_type:
        missing.append(
            "AUTH_TYPE (no default -- this endpoint is reachable from the public "
            "internet; set NONE explicitly if you really want no authentication)"
        )
    auth_type = (raw_auth_type or "NONE").upper()
    auth_token = _read_secret(env, "AUTH_TOKEN", "AUTH_TOKEN_FILE")
    auth_password = _read_secret(env, "AUTH_PASSWORD", "AUTH_PASSWORD_FILE")
    if auth_type == "BEARER" and not auth_token:
        missing.append("AUTH_TOKEN or AUTH_TOKEN_FILE (required when AUTH_TYPE=BEARER)")
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

    if missing:
        raise ConfigError("Missing required configuration: " + ", ".join(missing))

    return Config(
        listen_port=_int_env("LISTEN_PORT", env.get("LISTEN_PORT", "8080")),
        auth_type=auth_type,
        auth_username=env.get("AUTH_USERNAME", ""),
        auth_password=auth_password,
        auth_token=auth_token,
        trap_destinations=trap_destinations,
        v3_id_offset=v3_id_offset,
    )
