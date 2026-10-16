"""Webhook request authentication for appneta-ia-bridge's HTTP listener.

Supports the same NONE/BASIC/BEARER schemes generate-connector already
supports for the AppNeta connector's own webhook config, so the bridge can
accept whatever auth scheme a given customer's connector was set up with.
"""

import base64
import hmac


class AuthError(Exception):
    """Raised when a webhook request fails authentication."""


def validate_auth(headers: dict, auth_type: str, *, username: str = "", password: str = "", token: str = "") -> None:
    auth_type = (auth_type or "NONE").upper()
    if auth_type == "NONE":
        return

    header_value = headers.get("Authorization", "")

    if auth_type == "BEARER":
        expected = f"Bearer {token}"
        if not hmac.compare_digest(header_value.encode("utf-8"), expected.encode("utf-8")):
            raise AuthError("Invalid or missing bearer token")
    elif auth_type == "BASIC":
        expected_raw = f"{username}:{password}".encode("utf-8")
        expected = "Basic " + base64.b64encode(expected_raw).decode("ascii")
        if not hmac.compare_digest(header_value.encode("utf-8"), expected.encode("utf-8")):
            raise AuthError("Invalid or missing basic auth credentials")
    else:
        raise AuthError(f"Unsupported auth type: {auth_type!r}")
