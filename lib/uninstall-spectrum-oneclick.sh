#!/usr/bin/env bash
# Removes the OneClick-host text files that install-spectrum-oneclick.sh put in place. The state
# marker records the mode; the file list is that mode's own bundle directory, so exactly the
# files the installer wrote are removed and nothing else in CsPCause/CsEvFormat is touched.
#
# Usage: [SPECTRUM_HOME=<install dir>] ./uninstall-spectrum-oneclick.sh
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_spectrum_home.sh"
resolve_spectrum_home
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER="$SPECTRUM_HOME/SG-Support/.intelligent-alarms-oneclick-state"

if [ ! -f "$MARKER" ]; then
    echo "Nothing installed (no state marker at $MARKER) -- nothing to uninstall."
    exit 0
fi

mode="$({ grep '^mode=' "$MARKER" || true; } | cut -d= -f2)"
case "$mode" in
    vna) bundle="$LIB_DIR/../spectrum-install" ;;
    general) bundle="$LIB_DIR/../spectrum-install-general" ;;
    *) echo "ERROR: unrecognized or missing mode '$mode' in $MARKER -- manual reconciliation required." >&2; exit 1 ;;
esac

for f in "$bundle"/Prob*_en_US; do
    rm -f "$SPECTRUM_HOME/SG-Support/CsPCause/$(basename "$f")"
done
for f in "$bundle"/Event*_en_US; do
    rm -f "$SPECTRUM_HOME/SG-Support/CsEvFormat/$(basename "$f")"
done
rm -f "$MARKER"
echo "Uninstalled $mode OneClick-host files."
