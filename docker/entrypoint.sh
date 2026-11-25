#!/bin/bash
# Runs alarm_bridge and nginx side by side in one container, both as the same
# unprivileged user (nothing here needs root: nginx listens on :8443). If either
# process exits, the other is killed and the container exits -- no silent
# half-alive state. *_FILE secrets are read by alarm_bridge itself.
set -e

# TLS_ENABLED=false (an explicit opt-out, not the default) runs nginx as a
# plain-HTTP proxy instead -- for an environment where AppNeta's connector is
# already configured with an http:// URL and no real, publicly-trusted
# certificate is available yet. See README.md, "TLS termination", for the
# real cost of this (the webhook auth secret travels in cleartext) -- get a
# real cert and flip this back to true as soon as one exists.
TLS_ENABLED="${TLS_ENABLED:-true}"
TLS_DIR=/etc/appneta-ia-bridge/tls
if [ "$TLS_ENABLED" = "false" ]; then
    echo "WARNING: TLS_ENABLED=false -- serving plain HTTP on :8443. The webhook auth secret" >&2
    echo "         travels in cleartext over the network until this is a real, valid concern." >&2
    cp /etc/nginx/appneta-ia-bridge-templates/plain.conf /etc/nginx/conf.d/appneta-ia-bridge.conf
else
    # Say which of the three it is: nothing mounted, a directory this user cannot enter (needs the
    # execute bit, mode 755 or group ownership), or a file that is absent or unreadable.
    tls_problem=""
    if [ ! -d "$TLS_DIR" ]; then
        tls_problem="$TLS_DIR does not exist (no certificate directory is mounted)"
    elif [ ! -x "$TLS_DIR" ]; then
        tls_problem="$TLS_DIR exists but uid $(id -u) cannot enter it (the directory needs mode 755, or group ownership by that uid)"
    else
        for tls_file in cert.pem key.pem; do
            if [ ! -e "$TLS_DIR/$tls_file" ]; then
                tls_problem="$TLS_DIR/$tls_file is missing"
                break
            elif [ ! -r "$TLS_DIR/$tls_file" ]; then
                tls_problem="$TLS_DIR/$tls_file exists but uid $(id -u) cannot read it"
                break
            fi
        done
    fi
    if [ -n "$tls_problem" ]; then
        echo "ERROR: TLS_ENABLED=true (the default) but $tls_problem." >&2
        echo "       This container runs as uid $(id -u). Mount a real cert+key that uid can read, or set" >&2
        echo "       TLS_ENABLED=false if you understand and accept plain HTTP instead. Modes and owners" >&2
        echo "       as this container sees them (uid:gid):" >&2
        # shellcheck disable=SC2012  # ls -ldn on purpose: the line an operator pastes into a ticket
        ls -ldn "$TLS_DIR" "$TLS_DIR/cert.pem" "$TLS_DIR/key.pem" 2>&1 | sed 's/^/         /' >&2
        exit 1
    fi
    cp /etc/nginx/appneta-ia-bridge-templates/tls.conf /etc/nginx/conf.d/appneta-ia-bridge.conf
fi

python -m alarm_bridge &
ALARM_BRIDGE_PID=$!

# -e: nginx opens its compiled-in /var/log/nginx/error.log before it reads any config, which
# a non-root user cannot write; point that early log at stderr too.
nginx -e /dev/stderr -g "daemon off;" &
NGINX_PID=$!

# bash is PID 1 here, and PID 1 ignores SIGTERM unless a handler is installed:
# without this, `docker stop` hangs for its 10s grace period and then SIGKILLs
# both children. Forward the signal and exit with the conventional 128+15.
trap 'kill "$ALARM_BRIDGE_PID" "$NGINX_PID" 2>/dev/null || true; wait; exit 143' TERM INT

wait -n "$ALARM_BRIDGE_PID" "$NGINX_PID"
EXIT_CODE=$?
kill "$ALARM_BRIDGE_PID" "$NGINX_PID" 2>/dev/null || true
exit "$EXIT_CODE"
