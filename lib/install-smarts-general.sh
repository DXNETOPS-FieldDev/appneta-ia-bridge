#!/usr/bin/env bash
# Installs the general-mode (severity-keyed) AppNeta Intelligent Alarm trap-to-event wiring
# onto an existing VMware/Telco Cloud Smarts instance. Smarts has no VNA-equivalent per-path
# routing, so this is its only mode.
#
# Usage: SMARTS_HOME=/opt/InCharge/SAM/smarts ./install-smarts-general.sh
set -euo pipefail

SMARTS_HOME="${SMARTS_HOME:-/opt/InCharge/SAM/smarts}"
DMCTL="$SMARTS_HOME/bin/dmctl"
SM_SERVICE="$SMARTS_HOME/bin/sm_service"
TRAP_MGR_CONF="$SMARTS_HOME/conf/icoi/trap_mgr.conf"
MARKER_PATH="$SMARTS_HOME/conf/icoi/.intelligent-alarms-state"
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRAGMENT="$LIB_DIR/../smarts-install/trap_mgr.appneta-ia.conf"
MARKER_BEGIN="# appneta-ia-general v1 -- BEGIN -- AppNeta Intelligent Alarms"

# --- Preconditions -----------------------------------------------------------------
# Every check below exists because its failure mode is otherwise SILENT: the append and
# restart steps that follow report success unconditionally regardless of whether Smarts
# actually creates an event from the resulting trap. See CODING_STANDARDS.md's
# positive-control rule.

for bin in "$DMCTL" "$SM_SERVICE"; do
    if [ ! -x "$bin" ]; then
        echo "ERROR: expected executable not found: $bin (check SMARTS_HOME=$SMARTS_HOME)" >&2
        exit 1
    fi
done

if [ ! -f "$TRAP_MGR_CONF" ]; then
    echo "ERROR: $TRAP_MGR_CONF not found -- check SMARTS_HOME=$SMARTS_HOME" >&2
    exit 1
fi
if [ ! -w "$TRAP_MGR_CONF" ]; then
    echo "ERROR: $TRAP_MGR_CONF is not writable" >&2
    exit 1
fi

if ! "$DMCTL" -s INCHARGE-OI ping >/dev/null 2>&1; then
    echo "ERROR: INCHARGE-OI is not reachable ('dmctl -s INCHARGE-OI ping' failed)." >&2
    echo "Only INCHARGE-OI/INCHARGE-SA carry the SNMPTrapIndication/ICS_Notification model" >&2
    echo "these traps need -- without it, appending trap_mgr.conf and restarting the trap" >&2
    echo "receiver will succeed but silently create no events. Install it first:" >&2
    echo "  sm_service install ic-oi-server  (mirrors ic-am-pm-server's existing pattern)" >&2
    exit 1
fi

if ! "$SM_SERVICE" show 2>/dev/null | grep -q '^RUNNING ic-trapd-receiver$'; then
    echo "ERROR: ic-trapd-receiver is not in a clean RUNNING state ('sm_service show')." >&2
    echo "The restart step below (stop then start) requires a known starting state --" >&2
    echo "sm_service errors on 'stop' if the service is already stopped or stopping." >&2
    exit 1
fi

if [ -n "$(ip -6 addr show scope global 2>/dev/null)" ]; then
    echo "WARNING: a global IPv6 address is present on this host. The original Smarts" >&2
    echo "integration blocker was IPv6-address-selection related (the trap adapter's" >&2
    echo "reconnect logic got stuck on a link-local IPv6 address advertised by the broker)." >&2
    echo "Proceeding, but if traps stop producing events, this is the first thing to check." >&2
fi

# --- Install -------------------------------------------------------------------------

if grep -qF "$MARKER_BEGIN" "$TRAP_MGR_CONF"; then
    echo "trap_mgr.conf already contains the current appneta-ia-general block -- skipping append"
else
    echo "Appending appneta-ia-general block to $TRAP_MGR_CONF"
    printf '\n' >> "$TRAP_MGR_CONF"
    cat "$FRAGMENT" >> "$TRAP_MGR_CONF"
fi

echo "Restarting ic-trapd-receiver to reload trap_mgr.conf"
"$SM_SERVICE" stop ic-trapd-receiver
for _ in $(seq 1 30); do
    "$SM_SERVICE" show 2>/dev/null | grep -q '^NOT RUNNING ic-trapd-receiver$' && break
    sleep 1
done
"$SM_SERVICE" start ic-trapd-receiver
for _ in $(seq 1 30); do
    "$SM_SERVICE" show 2>/dev/null | grep -q '^RUNNING ic-trapd-receiver$' && break
    sleep 1
done
if ! "$SM_SERVICE" show 2>/dev/null | grep -q '^RUNNING ic-trapd-receiver$'; then
    echo "ERROR: ic-trapd-receiver did not come back RUNNING after restart." >&2
    exit 1
fi

# The appended block is a leading blank line + the fragment verbatim -- track lines/hash of
# exactly that, so uninstall's tail-match check lines up with what's physically in the file.
APPENDED_LINES="$(( $(wc -l < "$FRAGMENT") + 1 ))"
APPENDED_SHA="$( { printf '\n'; cat "$FRAGMENT"; } | sha256sum | cut -d' ' -f1)"
cat > "$MARKER_PATH" <<EOF
mode=general
version=1.0
installed=$(date -u +%Y-%m-%dT%H:%M:%SZ)
trap_mgr_lines_appended=${APPENDED_LINES}
trap_mgr_sha256=${APPENDED_SHA}
EOF
chmod 640 "$MARKER_PATH"

echo "Installed. MANDATORY next step, no exceptions: fire one test trap per severity and"
echo "confirm via 'dmctl -s INCHARGE-OI getInstances ICS_Notification' that an event"
echo "actually appears, then confirm the matching CLEARED trap removes it. A clean restart"
echo "is not proof anything works -- see smarts-install/README.md, 'Verifying delivery'."
