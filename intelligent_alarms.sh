#!/usr/bin/env bash
# Unified install/uninstall/status dispatcher for AppNeta Intelligent Alarms Spectrum/Smarts
# integration.
#
# Usage:
#   intelligent_alarms.sh --spectrum [--vna|--general] [install|uninstall|status]
#   intelligent_alarms.sh --smarts   [--general]        [install|uninstall|status]
#   intelligent_alarms.sh status                                # both targets, no mode needed
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORIG_ARGS=("$@")   # kept verbatim so the preflight can print a corrected command
source "$SCRIPT_DIR/lib/_preflight.sh"
source "$SCRIPT_DIR/lib/_spectrum_home.sh"
SPECTRUM_HOME="${SPECTRUM_HOME:-}"   # resolved on demand: explicit value, else SPECROOT from spectrum80.env (see lib/_spectrum_home.sh)
SMARTS_HOME="${SMARTS_HOME:-/opt/InCharge/SAM/smarts}"
SMARTS_MARKER="$SMARTS_HOME/conf/icoi/.intelligent-alarms-state"

usage() {
    cat >&2 <<'EOF'
Usage:
  intelligent_alarms.sh --spectrum [--vna|--general] [install|uninstall|status]
  intelligent_alarms.sh --smarts   [--general]        [install|uninstall|status]
  intelligent_alarms.sh status
EOF
    exit 1
}

require_spectrum_home() {
    resolve_spectrum_home
}

current_spectrum_mode() {
    local MARKER="$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/AppNetaAdmin/.intelligent-alarms-state"
    [ -f "$MARKER" ] && grep '^mode=' "$MARKER" | cut -d= -f2 || echo "none"
}

spectrum_status_line() {
    local mode
    mode="$(current_spectrum_mode)"
    if [ "$mode" = "none" ]; then
        echo "Spectrum: not installed"
    else
        echo "Spectrum: $mode installed"
    fi
}

current_smarts_mode() {
    [ -f "$SMARTS_MARKER" ] && grep '^mode=' "$SMARTS_MARKER" | cut -d= -f2 || echo "none"
}

smarts_status_line() {
    local mode
    mode="$(current_smarts_mode)"
    if [ "$mode" = "none" ]; then
        echo "Smarts:   not installed"
    else
        echo "Smarts:   $mode installed"
    fi
}

if [ "${1:-}" = "status" ] && [ $# -eq 1 ]; then
    require_spectrum_home
    spectrum_status_line
    smarts_status_line
    exit 0
fi

target=""
mode=""
action="install"

while [ $# -gt 0 ]; do
    case "$1" in
        --spectrum) target="spectrum" ;;
        --smarts) target="smarts" ;;
        --vna) mode="vna" ;;
        --general) mode="general" ;;
        install|uninstall|status) action="$1" ;;
        *) echo "ERROR: unrecognized argument '$1'" >&2; usage ;;
    esac
    shift
done

[ -z "$target" ] && usage

if [ "$mode" = "vna" ] && [ "$target" = "smarts" ]; then
    echo "ERROR: --smarts has no --vna mode (Smarts has no per-path routing capability)." >&2
    exit 1
fi

if [ "$target" = "smarts" ]; then
    if [ "$action" = "status" ]; then
        smarts_status_line
        exit 0
    fi

    if [ "$action" = "install" ] && [ -z "$mode" ]; then
        usage
    fi

    current_mode="$(current_smarts_mode)"
    if [ "$current_mode" != "none" ] && [ "$current_mode" != "$mode" ] && [ "$action" = "install" ]; then
        echo "Currently installed mode ($current_mode) differs from requested mode ($mode) -- switching."
        SMARTS_HOME="$SMARTS_HOME" "$SCRIPT_DIR/lib/uninstall-smarts-general.sh"
    fi

    case "$action" in
        install)
            SMARTS_HOME="$SMARTS_HOME" "$SCRIPT_DIR/lib/install-smarts-${mode}.sh"
            ;;
        uninstall)
            SMARTS_HOME="$SMARTS_HOME" "$SCRIPT_DIR/lib/uninstall-smarts-general.sh"
            ;;
    esac
    exit 0
fi

# target == spectrum from here on.
require_spectrum_home

if [ "$action" = "status" ]; then
    spectrum_status_line
    exit 0
fi

# Only install needs a mode: uninstall removes whatever the state marker says is
# installed, so `--spectrum uninstall` (mode omitted, as the usage line allows) must work.
if [ "$action" = "install" ] && [ -z "$mode" ]; then
    usage
fi

current_mode="$(current_spectrum_mode)"

# Refuse before touching anything if this user can't write the tree. An uninstall with nothing
# installed is a no-op, so don't fail it over ownership.
if [ "$action" = "install" ] || [ "$current_mode" != "none" ]; then
    preflight_spectrum_access "${ORIG_ARGS[@]}"
fi

if [ "$current_mode" != "none" ] && [ "$current_mode" != "$mode" ] && [ "$action" = "install" ]; then
    echo "Currently installed mode ($current_mode) differs from requested mode ($mode) -- switching."
    SPECTRUM_HOME="$SPECTRUM_HOME" "$SCRIPT_DIR/lib/uninstall-spectrum.sh"
fi

case "$action" in
    install)
        SPECTRUM_HOME="$SPECTRUM_HOME" "$SCRIPT_DIR/lib/install-spectrum-${mode}.sh"
        ;;
    uninstall)
        SPECTRUM_HOME="$SPECTRUM_HOME" "$SCRIPT_DIR/lib/uninstall-spectrum.sh"
        ;;
esac
