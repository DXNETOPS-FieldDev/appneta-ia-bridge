#!/usr/bin/env bash
# Installs the general-mode (non-path-associated) AppNeta Intelligent Alarm trap-to-alarm
# wiring onto an existing Broadcom DX NetOps Spectrum instance.
#
# Usage: [SPECTRUM_HOME=<install dir>] ./install-spectrum-general.sh
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_spectrum_home.sh"
resolve_spectrum_home
VENDOR_DIR="$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/AppNetaAdmin"
PCAUSE_DIR="$SPECTRUM_HOME/SG-Support/CsPCause"
EVFORMAT_DIR="$SPECTRUM_HOME/SG-Support/CsEvFormat"
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$LIB_DIR/../spectrum-install-general" && pwd)"
source "$LIB_DIR/_write_state_marker.sh"
source "$LIB_DIR/_install_helpers.sh"
source "$LIB_DIR/_compile_mibs.sh"

for dir in "$VENDOR_DIR" "$PCAUSE_DIR" "$EVFORMAT_DIR"; do
    if [ ! -d "$dir" ]; then
        echo "ERROR: expected directory not found: $dir" >&2
        exit 1
    fi
done

install_fragment "$SCRIPT_DIR/AlertMap.appneta-ia-general.fragment" "$VENDOR_DIR/AlertMap" "AlertMap"
install_fragment "$SCRIPT_DIR/EventDisp.appneta-ia-general.fragment" "$VENDOR_DIR/EventDisp" "EventDisp"

echo "Installing probable-cause files (alarm title text)"
for f in "$SCRIPT_DIR"/Prob*_en_US; do
    install_file "$f" "$PCAUSE_DIR/$(basename "$f")" 640
done

echo "Installing event-format files (alarm detail text)"
for f in "$SCRIPT_DIR"/Event*_en_US; do
    install_file "$f" "$EVFORMAT_DIR/$(basename "$f")" 640
done

ALERTMAP_LINES_ADDED="$(wc -l < "$SCRIPT_DIR/AlertMap.appneta-ia-general.fragment")"
ALERTMAP_SHA="$(sha256sum < "$SCRIPT_DIR/AlertMap.appneta-ia-general.fragment" | cut -d' ' -f1)"
EVENTDISP_LINES_ADDED="$(wc -l < "$SCRIPT_DIR/EventDisp.appneta-ia-general.fragment")"
EVENTDISP_SHA="$(sha256sum < "$SCRIPT_DIR/EventDisp.appneta-ia-general.fragment" | cut -d' ' -f1)"
write_state_marker "general" "$VENDOR_DIR/.intelligent-alarms-state" "1.0" \
    "$ALERTMAP_LINES_ADDED" "$ALERTMAP_SHA" "$EVENTDISP_LINES_ADDED" "$EVENTDISP_SHA"

compile_mibs "$SCRIPT_DIR/../mibs" || exit 1
finish_state_marker "$VENDOR_DIR/.intelligent-alarms-state" || exit 1

echo "Restart SpectroSERVER for the new AlertMap/EventDisp entries to take effect."
