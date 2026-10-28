#!/usr/bin/env bash
# Unified install/uninstall/status dispatcher for AppNeta Intelligent Alarms Spectrum/Smarts
# integration.
#
# Usage:
#   intelligent_alarms.sh --spectroserver [--vna|--general] [install|uninstall|status]
#   intelligent_alarms.sh --oneclick      [--vna|--general] [install|uninstall|status]
#   intelligent_alarms.sh --smarts   [--general]        [install|uninstall|status]
#   intelligent_alarms.sh status                                # both targets, no mode needed
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORIG_ARGS=("$@")   # kept verbatim so the preflight can print a corrected command
source "$SCRIPT_DIR/lib/_preflight.sh"
source "$SCRIPT_DIR/lib/_spectrum_home.sh"
source "$SCRIPT_DIR/lib/_logging.sh"
SPECTRUM_HOME="${SPECTRUM_HOME:-}"   # resolved on demand: explicit value, else SPECROOT from spectrum80.env (see lib/_spectrum_home.sh)
SMARTS_HOME="${SMARTS_HOME:-/opt/InCharge/SAM/smarts}"
SMARTS_MARKER="$SMARTS_HOME/conf/icoi/.intelligent-alarms-state"

usage() {
    cat >&2 <<'EOF'
Usage:
  intelligent_alarms.sh --spectroserver [--vna|--general] [install|uninstall|status]
  intelligent_alarms.sh --oneclick      [--vna|--general] [install|uninstall|status]
  intelligent_alarms.sh --smarts        [--general]        [install|uninstall|status]
  intelligent_alarms.sh status
EOF
    exit 1
}

require_spectrum_home() {
    resolve_spectrum_home
}

# marker_mode FILE: "none" if there is no marker, "unreadable" if there is one with no mode= line
# (e.g. a truncated write), else the mode. A marker we can't parse must never read as "not installed".
marker_mode() {
    local m
    [ -f "$1" ] || { echo none; return 0; }
    m="$({ grep '^mode=' "$1" || true; } | cut -d= -f2)"
    echo "${m:-unreadable}"
}

# incomplete_note FILE: a warning suffix when the installer wrote the marker but did not finish
# (state=incomplete); empty for a finished install and for markers from before the field existed.
incomplete_note() {
    [ -f "$1" ] || return 0
    if [ "$({ grep '^state=' "$1" || true; } | cut -d= -f2)" = "incomplete" ]; then
        echo " (INCOMPLETE install -- fix the error and re-run install)"
    fi
}

current_spectrum_mode() {
    marker_mode "$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/AppNetaAdmin/.intelligent-alarms-state"
}

spectrum_status_line() {
    local mode
    mode="$(current_spectrum_mode)"
    if [ "$mode" = "none" ]; then
        echo "Spectrum: not installed"
    elif [ "$mode" = "unreadable" ]; then
        echo "Spectrum: state marker present but unreadable -- manual reconciliation needed"
    else
        echo "Spectrum: $mode installed$(incomplete_note "$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/AppNetaAdmin/.intelligent-alarms-state")"
    fi
}

# Split installs: the OneClick host carries only the CsPCause/CsEvFormat text files.
current_oneclick_mode() {
    marker_mode "$SPECTRUM_HOME/SG-Support/.intelligent-alarms-oneclick-state"
}

oneclick_status_line() {
    local mode
    mode="$(current_oneclick_mode)"
    if [ "$mode" = "none" ]; then
        echo "Spectrum OneClick host: not installed"
    elif [ "$mode" = "unreadable" ]; then
        echo "Spectrum OneClick host: state marker present but unreadable -- manual reconciliation needed"
    else
        echo "Spectrum OneClick host: $mode installed$(incomplete_note "$SPECTRUM_HOME/SG-Support/.intelligent-alarms-oneclick-state")"
    fi
}

current_smarts_mode() {
    marker_mode "$SMARTS_MARKER"
}

smarts_status_line() {
    local mode
    mode="$(current_smarts_mode)"
    if [ "$mode" = "none" ]; then
        echo "Smarts:   not installed"
    elif [ "$mode" = "unreadable" ]; then
        echo "Smarts:   state marker present but unreadable -- manual reconciliation needed"
    else
        echo "Smarts:   $mode installed"
    fi
}

if [ "${1:-}" = "status" ] && [ $# -eq 1 ]; then
    require_spectrum_home
    spectrum_status_line
    # The OneClick line only matters on a OneClick-only host; keep it out of everyone else's output.
    if [ "$(current_oneclick_mode)" != "none" ]; then
        oneclick_status_line
    fi
    smarts_status_line
    exit 0
fi

target=""
mode=""
action="install"

while [ $# -gt 0 ]; do
    case "$1" in
        --spectrum)
            echo "ERROR: --spectrum was replaced by --spectroserver (SpectroSERVER host, or a single-host install) and --oneclick (OneClick-only host)." >&2
            exit 1 ;;
        --spectroserver|--oneclick|--smarts)
            new_target="${1#--}"
            if [ -n "$target" ] && [ "$target" != "$new_target" ]; then
                echo "ERROR: choose one of --spectroserver, --oneclick, --smarts (got --$target and --$new_target)." >&2
                exit 1
            fi
            target="$new_target" ;;
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

# install and uninstall leave a log; status is read-only and does not. A Spectrum run as root is
# refused by the preflight, so don't let it create a root-owned logs/ directory that would lock the
# corrected (non-root) run out of logging.
if [ "$action" != "status" ] && { [ "$target" = "smarts" ] || [ "$(id -un)" != "root" ]; }; then
    start_logging "$target" "$action"
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

# target is spectroserver or oneclick from here on.
require_spectrum_home

# OneClick-only host of a split install: just the text files (see lib/install-spectrum-oneclick.sh).
if [ "$target" = "oneclick" ]; then
    if [ "$action" = "status" ]; then
        oneclick_status_line
        exit 0
    fi
    if [ "$action" = "install" ] && [ -z "$mode" ]; then
        usage
    fi
    # A host with SS/ gets these text files from --spectroserver, which owns them: --oneclick install
    # there would duplicate them and --oneclick uninstall would delete them from under the SS install.
    # (Uninstalling a stray OneClick marker on an SS host with no SS install is still allowed.)
    if [ -d "$SPECTRUM_HOME/SS" ] && { [ "$action" = "install" ] || [ "$(current_spectrum_mode)" != "none" ]; }; then
        echo "ERROR: $SPECTRUM_HOME has a SpectroSERVER (SS/); --oneclick is for a OneClick-only host." >&2
        echo "       Use --spectroserver here: it installs these text files too." >&2
        exit 1
    fi
    preflight_oneclick_access "${ORIG_ARGS[@]}"
    current_mode="$(current_oneclick_mode)"
    if [ "$current_mode" != "none" ] && [ "$current_mode" != "$mode" ] && [ "$action" = "install" ]; then
        echo "Currently installed mode ($current_mode) differs from requested mode ($mode) -- switching."
        SPECTRUM_HOME="$SPECTRUM_HOME" "$SCRIPT_DIR/lib/uninstall-spectrum-oneclick.sh"
    fi
    case "$action" in
        install) MODE="$mode" SPECTRUM_HOME="$SPECTRUM_HOME" "$SCRIPT_DIR/lib/install-spectrum-oneclick.sh" ;;
        uninstall) SPECTRUM_HOME="$SPECTRUM_HOME" "$SCRIPT_DIR/lib/uninstall-spectrum-oneclick.sh" ;;
    esac
    exit 0
fi

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
        # No tomcat/ means OneClick is not on this host (a split install's SpectroSERVER host). OneClick reads
        # the alarm text files from its own host, so it needs its own run; we can't see that host from here.
        if [ ! -d "$SPECTRUM_HOME/tomcat" ]; then
            echo ""
            echo "REMINDER: OneClick is not installed on this host. If OneClick runs on another host, run"
            echo "    ./intelligent_alarms.sh --oneclick --${mode} install"
            echo "there as well, or its console will show \"file Prob... was not found\" for these alarms."
        fi
        ;;
    uninstall)
        SPECTRUM_HOME="$SPECTRUM_HOME" "$SCRIPT_DIR/lib/uninstall-spectrum.sh"
        ;;
esac
