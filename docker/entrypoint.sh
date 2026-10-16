#!/bin/bash
# Runs alarm_bridge (as the unprivileged appuser) and nginx (needs root to
# bind :443) side by side in one container. If either process exits, the
# other is killed and the container exits -- no silent half-alive state.
set -e

# TLS_ENABLED=false (an explicit opt-out, not the default) runs nginx as a
# plain-HTTP proxy instead -- for an environment where AppNeta's connector is
# already configured with an http:// URL and no real, publicly-trusted
# certificate is available yet. See README.md, "TLS termination", for the
# real cost of this (the webhook auth secret travels in cleartext) -- get a
# real cert and flip this back to true as soon as one exists.
TLS_ENABLED="${TLS_ENABLED:-true}"
if [ "$TLS_ENABLED" = "false" ]; then
    echo "WARNING: TLS_ENABLED=false -- serving plain HTTP on :443. The webhook auth secret" >&2
    echo "         travels in cleartext over the network until this is a real, valid concern." >&2
    cp /etc/nginx/appneta-ia-bridge-templates/plain.conf /etc/nginx/conf.d/appneta-ia-bridge.conf
else
    if [ ! -r /etc/appneta-ia-bridge/tls/cert.pem ] || [ ! -r /etc/appneta-ia-bridge/tls/key.pem ]; then
        echo "ERROR: TLS_ENABLED=true (the default) but /etc/appneta-ia-bridge/tls/cert.pem or" >&2
        echo "       key.pem is missing/unreadable. Mount a real cert+key, or set" >&2
        echo "       TLS_ENABLED=false if you understand and accept plain HTTP instead." >&2
        exit 1
    fi
    cp /etc/nginx/appneta-ia-bridge-templates/tls.conf /etc/nginx/conf.d/appneta-ia-bridge.conf
fi

# Resolve file-based secrets (AUTH_TOKEN_FILE/AUTH_PASSWORD_FILE) as root,
# before dropping to appuser -- this container's own root privilege is what
# reads them, so the mounted secret file can stay root-owned/chmod 600 on the
# host rather than needing to be readable by appuser's uid.
if [ -n "$AUTH_TOKEN_FILE" ]; then
    if [ ! -r "$AUTH_TOKEN_FILE" ]; then
        echo "ERROR: AUTH_TOKEN_FILE=$AUTH_TOKEN_FILE is not readable" >&2
        exit 1
    fi
    export AUTH_TOKEN
    AUTH_TOKEN="$(cat "$AUTH_TOKEN_FILE")"
    # alarm_bridge's own config also honors AUTH_TOKEN_FILE (and would try to
    # read it itself, as appuser, and fail) -- unset it now that we've already
    # resolved the value as root.
    unset AUTH_TOKEN_FILE
fi
if [ -n "$AUTH_PASSWORD_FILE" ]; then
    if [ ! -r "$AUTH_PASSWORD_FILE" ]; then
        echo "ERROR: AUTH_PASSWORD_FILE=$AUTH_PASSWORD_FILE is not readable" >&2
        exit 1
    fi
    export AUTH_PASSWORD
    AUTH_PASSWORD="$(cat "$AUTH_PASSWORD_FILE")"
    unset AUTH_PASSWORD_FILE
fi

su -s /bin/bash appuser -c "python -m alarm_bridge" &
ALARM_BRIDGE_PID=$!

nginx -g "daemon off;" &
NGINX_PID=$!

wait -n "$ALARM_BRIDGE_PID" "$NGINX_PID"
EXIT_CODE=$?
kill "$ALARM_BRIDGE_PID" "$NGINX_PID" 2>/dev/null || true
exit "$EXIT_CODE"
