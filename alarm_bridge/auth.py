"""Webhook request authentication for appneta-ia-bridge's HTTP listener.

Every request must authenticate; there is deliberately no "no authentication"
mode. BEARER and BASIC match the schemes the AppNeta connector's own webhook
config supports (the same ones generate-connector emits). HEADER checks a
secret carried in an operator-named custom header, for deployments where a
front proxy or gateway consumes the Authorization header.

Anything unrecognised -- including NONE, an empty type, or a missing secret --
fails closed.
"""

import base64
import hmac
from email.message import Message
from typing import Union
from collections.abc import Mapping

SUPPORTED_AUTH_TYPES = ("BEARER", "BASIC", "HEADER")


class AuthError(Exception):
    """Raised when a webhook request fails authentication."""


def _matches(actual: str, expected: str) -> bool:
    return hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8"))


def validate_auth(
    headers: Union[Mapping[str, str], Message],
    auth_type: str,
    *,
    username: str = "",
    password: str = "",
    token: str = "",
    header_name: str = "",
) -> None:
    auth_type = (auth_type or "").upper()
    if auth_type not in SUPPORTED_AUTH_TYPES:
        raise AuthError(f"Unsupported auth type: {auth_type!r}")

    if auth_type == "BEARER":
        if not token:
            raise AuthError("Bearer token is not configured")
        if not _matches(headers.get("Authorization", ""), f"Bearer {token}"):
            raise AuthError("Invalid or missing bearer token")
    elif auth_type == "BASIC":
        if not password:
            raise AuthError("Basic auth password is not configured")
        expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        if not _matches(headers.get("Authorization", ""), expected):
            raise AuthError("Invalid or missing basic auth credentials")
    else:
        if not header_name or not token:
            raise AuthError("Header auth is not fully configured")
        if not _matches(headers.get(header_name, ""), token):
            raise AuthError(f"Invalid or missing {header_name} header")
