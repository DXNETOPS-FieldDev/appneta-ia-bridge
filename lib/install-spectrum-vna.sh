#!/usr/bin/env bash
# Installs the AppNeta Intelligent Alarm trap-to-alarm wiring onto an
# existing Broadcom DX NetOps Spectrum instance. Run as a user with write
# access to $SPECTRUM_HOME/SS/CsVendor (typically the `spectrum` user).
#
# Usage: [SPECTRUM_HOME=<install dir>] ./install-spectrum-vna.sh
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_spectrum_home.sh"
resolve_spectrum_home
VENDOR_DIR="$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/AppNetaAdmin"
NETWORKPATH_DIR="$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/NetworkPath"
WEBPATH_DIR="$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/WebPath"
PCAUSE_DIR="$SPECTRUM_HOME/SG-Support/CsPCause"
EVFORMAT_DIR="$SPECTRUM_HOME/SG-Support/CsEvFormat"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../spectrum-install" && pwd)"
source "$(dirname "${BASH_SOURCE[0]}")/_install_helpers.sh"
source "$(dirname "${BASH_SOURCE[0]}")/_write_state_marker.sh"

for dir in "$VENDOR_DIR" "$PCAUSE_DIR" "$EVFORMAT_DIR"; do
    if [ ! -d "$dir" ]; then
        echo "ERROR: expected directory not found: $dir" >&2
        echo "Is SPECTRUM_HOME correct, and does an AppNetaAdmin model type already exist?" >&2
        exit 1
    fi
done

# Refuse to install placeholder content, so an incomplete checkout can't be
# installed against a real Spectrum instance.
SOURCE_FILES=(
    "$SCRIPT_DIR/AlertMap.appneta-ia.fragment"
    "$SCRIPT_DIR/EventDisp.appneta-ia.fragment"
    "$SCRIPT_DIR/NetworkPath.EventDisp"
    "$SCRIPT_DIR/WebPath.EventDisp"
)
for f in "${SOURCE_FILES[@]}"; do
    if grep -q "STUB-DO-NOT-INSTALL\|PLACEHOLDER-DO-NOT-INSTALL" "$f" 2>/dev/null; then
        echo "ERROR: $f is marked as a placeholder (STUB-DO-NOT-INSTALL) and must not be installed." >&2
        echo "Replace it with real content before running this script." >&2
        exit 1
    fi
done

install_fragment "$SCRIPT_DIR/AlertMap.appneta-ia.fragment" "$VENDOR_DIR/AlertMap" "AlertMap"
install_fragment "$SCRIPT_DIR/EventDisp.appneta-ia.fragment" "$VENDOR_DIR/EventDisp" "EventDisp"

ALERTMAP_LINES_ADDED="$(wc -l < "$SCRIPT_DIR/AlertMap.appneta-ia.fragment")"
ALERTMAP_SHA="$(sha256sum < "$SCRIPT_DIR/AlertMap.appneta-ia.fragment" | cut -d' ' -f1)"
EVENTDISP_LINES_ADDED="$(wc -l < "$SCRIPT_DIR/EventDisp.appneta-ia.fragment")"
EVENTDISP_SHA="$(sha256sum < "$SCRIPT_DIR/EventDisp.appneta-ia.fragment" | cut -d' ' -f1)"
write_state_marker "vna" "$VENDOR_DIR/.intelligent-alarms-state" "1.0" \
    "$ALERTMAP_LINES_ADDED" "$ALERTMAP_SHA" "$EVENTDISP_LINES_ADDED" "$EVENTDISP_SHA"

# EventDisp alarm rules are scoped to the model type that receives the event -- the
# AppNetaAdmin/EventDisp rules just installed above never fire for an event Field 9 routes onto
# a NetworkPath/WebPath model. These are whole small files we own outright, not a shared vendor file, so a plain
# copy (not the append-with-conflict-detection fragment logic above) is enough.
echo "Installing model-type-scoped EventDisp for NetworkPath and WebPath"
mkdir -p "$NETWORKPATH_DIR" "$WEBPATH_DIR"
install_file "$SCRIPT_DIR/NetworkPath.EventDisp" "$NETWORKPATH_DIR/EventDisp" 644
install_file "$SCRIPT_DIR/WebPath.EventDisp" "$WEBPATH_DIR/EventDisp" 644

echo "Installing probable-cause files (alarm title text)"
for f in "$SCRIPT_DIR"/Prob*_en_US; do
    install_file "$f" "$PCAUSE_DIR/$(basename "$f")" 640
done

echo "Installing event-format files (alarm detail text)"
for f in "$SCRIPT_DIR"/Event*_en_US; do
    install_file "$f" "$EVFORMAT_DIR/$(basename "$f")" 640
done

SEVERITYMAP_DIR="$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/SeverityMaps"
echo "Installing severity map to $SEVERITYMAP_DIR"
mkdir -p "$SEVERITYMAP_DIR"
install_file "$SCRIPT_DIR/SeverityMaps.IntelligentAlarmSeverity" "$SEVERITYMAP_DIR/IntelligentAlarmSeverity"

echo "Copy $SCRIPT_DIR/../mibs/APPNETA-INTELLIGENT-ALARM-MIB.mib into your MIB compiler"
echo "and load it via OneClick Administration (or the mibtoolsutil CLI), then restart"
echo "SpectroSERVER for the new AlertMap/EventDisp/SeverityMap entries to take"
echo "effect. See spectrum-install/README.md for details."
