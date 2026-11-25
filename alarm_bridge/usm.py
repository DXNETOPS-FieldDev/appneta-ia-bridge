"""SNMPv3 User-based Security Model for sending traps: key localization and
authentication (RFC 3414, SHA-2 per RFC 7860) and AES-128-CFB privacy (RFC 3826).

Authentication uses only hashlib/hmac. AES comes from the `cryptography`
package, imported lazily so a v2c-only deployment never needs it; the image
installs the RHEL build linked against the host OpenSSL. No hand-rolled cipher.
"""

import hashlib
import hmac
import importlib.util
import os
import time
from dataclasses import dataclass
from functools import lru_cache

# protocol name -> (hashlib name, msgAuthenticationParameters length in bytes)
AUTH_PROTOCOLS = {
    "SHA": ("sha1", 12),
    "SHA256": ("sha256", 24),
    "SHA384": ("sha384", 32),
    "SHA512": ("sha512", 48),
}
# SHA-1 is not an approved USM authentication algorithm for the us-secure profile.
US_SECURE_AUTH_PROTOCOLS = ("SHA256", "SHA384", "SHA512")

MIN_PASSWORD_LENGTH = 8  # RFC 3414 section 11.2
AES_SALT_LENGTH = 8
AES_KEY_LENGTH = 16

# msgAuthoritativeEngineBoots counts "reboots" and must only ever grow, but the
# container filesystem is ephemeral. Boots is the start time in seconds since
# the 2020-era epoch below, 1577836800 (fits the 31-bit field until 2088), so every restart is higher
# than the last without any state to persist; engine time counts up from start.
_BOOTS_EPOCH = 1577836800
_START_WALL = int(time.time())
_START_MONOTONIC = time.monotonic()


@dataclass(frozen=True)
class V3Credentials:
    user: str
    engine_id: bytes
    auth_protocol: str  # key of AUTH_PROTOCOLS
    auth_password: str
    priv_password: str  # privacy is always AES-128: this sender only does authPriv


def engine_boots() -> int:
    return _START_WALL - _BOOTS_EPOCH


def engine_time() -> int:
    return int(time.monotonic() - _START_MONOTONIC)


@lru_cache(maxsize=32)
def localized_key(password: str, engine_id: bytes, auth_protocol: str) -> bytes:
    """RFC 3414 A.2: hash 1 MiB of the repeated password, then localize to the engine.
    The privacy key is localized with the same hash as the authentication key."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"SNMPv3 password must be at least {MIN_PASSWORD_LENGTH} characters")
    hash_name = AUTH_PROTOCOLS[auth_protocol][0]
    pw = password.encode("utf-8")
    stream = (pw * (1048576 // len(pw) + 1))[:1048576]
    ku = hashlib.new(hash_name, stream).digest()
    return hashlib.new(hash_name, ku + engine_id + ku).digest()


def auth_params_length(auth_protocol: str) -> int:
    return AUTH_PROTOCOLS[auth_protocol][1]


def authenticate(creds: V3Credentials, message: bytes) -> bytes:
    """HMAC over the whole message (authentication parameters zeroed), truncated."""
    hash_name, mac_len = AUTH_PROTOCOLS[creds.auth_protocol]
    key = localized_key(creds.auth_password, creds.engine_id, creds.auth_protocol)
    return hmac.new(key, message, hash_name).digest()[:mac_len]


def new_salt() -> bytes:
    return os.urandom(AES_SALT_LENGTH)


def _cfb(iv: bytes):
    """CFB mode object. Newer cryptography moved CFB to the decrepit namespace and
    warns on the old path; older releases only have the old path."""
    try:
        from cryptography.hazmat.decrepit.ciphers.modes import CFB
    except ImportError:
        from cryptography.hazmat.primitives.ciphers.modes import CFB
    return CFB(iv)


def encrypt(creds: V3Credentials, boots: int, eng_time: int, salt: bytes, plaintext: bytes) -> bytes:
    """AES-128-CFB (RFC 3826): IV = boots || time || salt, key = first 16 bytes of the
    localized privacy key."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

    key = localized_key(creds.priv_password, creds.engine_id, creds.auth_protocol)[:AES_KEY_LENGTH]
    iv = boots.to_bytes(4, "big") + eng_time.to_bytes(4, "big") + salt
    encryptor = Cipher(algorithms.AES(key), _cfb(iv)).encryptor()
    return encryptor.update(plaintext) + encryptor.finalize()


def crypto_available() -> bool:
    return importlib.util.find_spec("cryptography") is not None
