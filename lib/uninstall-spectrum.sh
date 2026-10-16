#!/usr/bin/env bash
# Removes whichever intelligent-alarms mode (vna or general) is currently installed,
# using the state marker to know exactly what to remove -- never re-derives this from
# AlertMap/EventDisp content.
#
# Usage: [SPECTRUM_HOME=<install dir>] ./uninstall-spectrum.sh
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/_spectrum_home.sh"
resolve_spectrum_home
VENDOR_DIR="$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/AppNetaAdmin"
NETWORKPATH_DIR="$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/NetworkPath"
WEBPATH_DIR="$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/WebPath"
PCAUSE_DIR="$SPECTRUM_HOME/SG-Support/CsPCause"
EVFORMAT_DIR="$SPECTRUM_HOME/SG-Support/CsEvFormat"
MARKER="$VENDOR_DIR/.intelligent-alarms-state"
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$MARKER" ]; then
    echo "Nothing installed (no state marker at $MARKER) -- nothing to uninstall."
    exit 0
fi

# shellcheck disable=SC1090
mode="$(grep '^mode=' "$MARKER" | cut -d= -f2)"
# wc -l left-pads its output with whitespace (e.g. "    1782"), and that padding is
# preserved verbatim when the install scripts write these fields into the marker --
# strip it here so it can't break tail's argument parsing or a numeric comparison.
alertmap_lines="$(grep '^alertmap_lines_appended=' "$MARKER" | cut -d= -f2 | tr -d '[:space:]')"
alertmap_sha="$(grep '^alertmap_sha256=' "$MARKER" | cut -d= -f2)"
eventdisp_lines="$(grep '^eventdisp_lines_appended=' "$MARKER" | cut -d= -f2 | tr -d '[:space:]')"
eventdisp_sha="$(grep '^eventdisp_sha256=' "$MARKER" | cut -d= -f2)"

if [ -z "$mode" ] || [ -z "$alertmap_lines" ] || [ -z "$eventdisp_lines" ]; then
    echo "ERROR: state marker at $MARKER is malformed (missing required fields)." >&2
    echo "Manual reconciliation required -- refusing to guess." >&2
    exit 1
fi

verify_and_truncate() {
    local target="$1" expected_lines="$2" expected_sha="$3" label="$4"
    local actual_sha
    actual_sha="$(tail -n "$expected_lines" "$target" | sha256sum | cut -d' ' -f1)"
    if [ "$actual_sha" != "$expected_sha" ]; then
        echo "ERROR: $label's last $expected_lines lines don't match what was installed" >&2
        echo "(expected sha256 $expected_sha, got $actual_sha)." >&2
        echo "Something was appended after install or the file was hand-edited." >&2
        echo "Refusing to truncate -- manual reconciliation required." >&2
        exit 1
    fi
    # `head -n -N` (GNU-only "all but the last N lines") isn't portable -- BSD/macOS
    # head rejects the negative count. Compute the positive keep-count instead so
    # this works on both.
    local total_lines keep_lines
    total_lines="$(wc -l < "$target" | tr -d '[:space:]')"
    keep_lines=$((total_lines - expected_lines))
    if [ "$keep_lines" -le 0 ]; then
        : > "$target"
    else
        head -n "$keep_lines" "$target" > "${target}.tmp" && mv "${target}.tmp" "$target"
    fi
    echo "Removed $label's $mode-mode content ($expected_lines lines)."
}

verify_and_truncate "$VENDOR_DIR/AlertMap" "$alertmap_lines" "$alertmap_sha" "AlertMap"
verify_and_truncate "$VENDOR_DIR/EventDisp" "$eventdisp_lines" "$eventdisp_sha" "EventDisp"

if [ "$mode" = "vna" ]; then
    # Model-type-scoped EventDisp for NetworkPath/WebPath -- whole small files we own
    # outright (see install-spectrum-vna.sh), removed the same way as Prob*/Event* below
    # rather than the verify-and-truncate treatment used for the shared AppNetaAdmin files.
    rm -f "$NETWORKPATH_DIR/EventDisp" "$WEBPATH_DIR/EventDisp"
    rm -f "$SPECTRUM_HOME"/SG-Support/CsPCause/Prob0673215[02468ace]_en_US
    rm -f "$SPECTRUM_HOME"/SG-Support/CsPCause/Prob06732180_en_US
    # Both parities: even codes are the raised tier, odd the cleared tier.
    # [0-3] on the 0673217x line deliberately stops before 0x06732174, which
    # is general mode's range and is removed by the 'general' branch below.
    rm -f "$SPECTRUM_HOME"/SG-Support/CsEvFormat/Event0673215[0-9a-f]_en_US
    rm -f "$SPECTRUM_HOME"/SG-Support/CsEvFormat/Event0673216[0-9a-f]_en_US
    rm -f "$SPECTRUM_HOME"/SG-Support/CsEvFormat/Event0673217[0-3]_en_US
    # 0x6732180-83: catch-all (custom rule) family.
    rm -f "$SPECTRUM_HOME"/SG-Support/CsEvFormat/Event0673218[0-3]_en_US
elif [ "$mode" = "general" ]; then
    rm -f "$PCAUSE_DIR/Prob06732174_en_US" "$PCAUSE_DIR/Prob06732176_en_US"
    # Raised (even) and cleared (odd) tiers.
    rm -f "$EVFORMAT_DIR"/Event0673217{4,5,6,7,8,a}_en_US
else
    echo "ERROR: unknown mode '$mode' in state marker -- refusing to remove text files blindly." >&2
    exit 1
fi

rm -f "$MARKER"
echo "Uninstalled $mode mode cleanly."
