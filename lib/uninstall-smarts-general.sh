#!/usr/bin/env bash
# Removes exactly what install-smarts-general.sh added, verified by checksum against the
# state marker -- refuses (changes nothing) if trap_mgr.conf's tail doesn't match what the
# marker recorded, e.g. because something else was appended after install or the file was
# hand-edited. Mirrors lib/uninstall-spectrum.sh's checksum-based removal.
#
# Usage: SMARTS_HOME=/opt/InCharge/SAM/smarts ./uninstall-smarts-general.sh
set -euo pipefail

SMARTS_HOME="${SMARTS_HOME:-/opt/InCharge/SAM/smarts}"
SM_SERVICE="$SMARTS_HOME/bin/sm_service"
TRAP_MGR_CONF="$SMARTS_HOME/conf/icoi/trap_mgr.conf"
MARKER_PATH="$SMARTS_HOME/conf/icoi/.intelligent-alarms-state"

if [ ! -f "$MARKER_PATH" ]; then
    echo "Nothing installed (no state marker at $MARKER_PATH) -- nothing to do."
    exit 0
fi

# shellcheck disable=SC1090
mode="$(grep '^mode=' "$MARKER_PATH" | cut -d= -f2)"
lines_appended="$(grep '^trap_mgr_lines_appended=' "$MARKER_PATH" | cut -d= -f2)"
recorded_sha="$(grep '^trap_mgr_sha256=' "$MARKER_PATH" | cut -d= -f2)"

if [ "$mode" != "general" ] || [ -z "$lines_appended" ] || [ -z "$recorded_sha" ]; then
    echo "ERROR: $MARKER_PATH is malformed or from an unrecognized mode ('$mode')." >&2
    echo "Refusing to guess what to remove -- reconcile manually." >&2
    exit 1
fi

total_lines="$(wc -l < "$TRAP_MGR_CONF")"
if [ "$total_lines" -lt "$lines_appended" ]; then
    echo "ERROR: $TRAP_MGR_CONF has fewer lines than the marker says were appended." >&2
    echo "Refusing to truncate -- reconcile manually." >&2
    exit 1
fi

tail_sha="$(tail -n "$lines_appended" "$TRAP_MGR_CONF" | sha256sum | cut -d' ' -f1)"
if [ "$tail_sha" != "$recorded_sha" ]; then
    echo "ERROR: $TRAP_MGR_CONF's last $lines_appended lines don't match what the marker" >&2
    echo "recorded -- something else was appended after install, or the file was hand-edited." >&2
    echo "Refusing to truncate the wrong lines. Reconcile manually." >&2
    exit 1
fi

keep_lines="$(( total_lines - lines_appended ))"
tmp="$(mktemp)"
head -n "$keep_lines" "$TRAP_MGR_CONF" > "$tmp"
mv "$tmp" "$TRAP_MGR_CONF"
echo "Removed appneta-ia-general block from $TRAP_MGR_CONF"

if [ -x "$SM_SERVICE" ]; then
    echo "Restarting ic-trapd-receiver to drop the removed block"
    "$SM_SERVICE" stop ic-trapd-receiver
    for _ in $(seq 1 30); do
        "$SM_SERVICE" show 2>/dev/null | grep -q '^NOT RUNNING ic-trapd-receiver$' && break
        sleep 1
    done
    "$SM_SERVICE" start ic-trapd-receiver
fi

rm -f "$MARKER_PATH"
echo "Uninstalled."
