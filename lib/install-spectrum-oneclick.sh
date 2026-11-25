#!/usr/bin/env bash
# Installs the OneClick-host half of the AppNeta Intelligent Alarms Spectrum integration:
# the probable-cause (CsPCause) and event-format (CsEvFormat) text files, nothing else.
# For a split Spectrum install, run this on the OneClick web server host; run the normal
# --spectroserver install on the SpectroSERVER host. OneClick reads these two directories from
# its own SG-Support, so without them the console shows "file Prob... was not found on this
# server" / "no event format file exists".
#
# Usage: MODE=<vna|general> [SPECTRUM_HOME=<install dir>] ./install-spectrum-oneclick.sh
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_spectrum_home.sh"
resolve_spectrum_home
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$LIB_DIR/_install_helpers.sh"
source "$LIB_DIR/_compile_mibs.sh"
source "$LIB_DIR/_write_state_marker.sh"

case "${MODE:-}" in
    vna) SCRIPT_DIR="$LIB_DIR/../spectrum-install" ;;
    general) SCRIPT_DIR="$LIB_DIR/../spectrum-install-general" ;;
    *) echo "ERROR: MODE must be 'vna' or 'general', got '${MODE:-}'" >&2; exit 1 ;;
esac
SCRIPT_DIR="$(cd "$SCRIPT_DIR" && pwd)"
PCAUSE_DIR="$SPECTRUM_HOME/SG-Support/CsPCause"
EVFORMAT_DIR="$SPECTRUM_HOME/SG-Support/CsEvFormat"
MARKER="$SPECTRUM_HOME/SG-Support/.intelligent-alarms-oneclick-state"

for dir in "$PCAUSE_DIR" "$EVFORMAT_DIR"; do
    if [ ! -d "$dir" ]; then
        echo "ERROR: expected directory not found: $dir" >&2
        exit 1
    fi
done

echo "Installing probable-cause files (alarm title text)"
for f in "$SCRIPT_DIR"/Prob*_en_US; do
    install_file "$f" "$PCAUSE_DIR/$(basename "$f")" 640
done

echo "Installing event-format files (alarm detail text)"
for f in "$SCRIPT_DIR"/Event*_en_US; do
    install_file "$f" "$EVFORMAT_DIR/$(basename "$f")" 640
done

cat > "$MARKER" <<EOF
mode=${MODE}
installed=$(date -u +%Y-%m-%dT%H:%M:%SZ)
state=incomplete
EOF
chmod 640 "$MARKER"

compile_mibs "$SCRIPT_DIR/../mibs" || exit 1
finish_state_marker "$MARKER" || exit 1

echo "OneClick-host files installed (${MODE}); OneClick reads them live, no restart needed."
echo "Run --spectroserver install on the SpectroSERVER host."
