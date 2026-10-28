# lib/_preflight.sh
# Sourced by intelligent_alarms.sh. Checks, BEFORE anything is modified, that the current
# user can write every Spectrum path an install/uninstall touches, and stops with the
# corrected command when it can't. File ownership in the field varies (spectrum, root, an
# admin account), and a half-finished install is worse than a refused one.

_owner_of() { ls -ld "$1" | awk '{print $3}'; }
_group_of() { ls -ld "$1" | awk '{print $4}'; }
_mode_of()  { ls -ld "$1" | awk '{print $1}'; }

# preflight_spectrum_access ORIGINAL_ARGV...   (the script's own argv, for the corrected command)
preflight_spectrum_access() {
    local ss_dir="$SPECTRUM_HOME/SS"
    if [ ! -d "$ss_dir" ]; then
        echo "ERROR: $ss_dir not found. Is SPECTRUM_HOME ($SPECTRUM_HOME) the Spectrum install dir?" >&2
        echo "       On the OneClick-only host of a split install, use --oneclick instead of --spectroserver (installs just the text files)." >&2
        exit 1
    fi

    local me expected_user expected_group
    me="$(id -un)"
    expected_user="$(_owner_of "$ss_dir")"
    expected_group="$(_group_of "$ss_dir")"

    local sdn="$ss_dir/CsVendor/SDN_INTEG"
    local vendor="$sdn/AppNetaAdmin" pcause="$SPECTRUM_HOME/SG-Support/CsPCause"
    local evformat="$SPECTRUM_HOME/SG-Support/CsEvFormat"

    # Directories we create into or delete from. One that doesn't exist yet is created by the
    # installer under SDN_INTEG, so SDN_INTEG itself must be writable in that case.
    local dirs=() files=() p
    for p in "$vendor" "$pcause" "$evformat" "$sdn/NetworkPath" "$sdn/WebPath" "$sdn/SeverityMaps"; do
        if [ -d "$p" ]; then
            dirs+=("$p")
        elif [ "$p" = "$sdn/NetworkPath" ] || [ "$p" = "$sdn/WebPath" ] || [ "$p" = "$sdn/SeverityMaps" ]; then
            [ -d "$sdn" ] && dirs+=("$sdn")
        fi
    done

    # Existing files the installer overwrites/appends to, or the uninstaller truncates/removes.
    for p in "$vendor/AlertMap" "$vendor/EventDisp" "$vendor/.intelligent-alarms-state" \
             "$sdn/NetworkPath/EventDisp" "$sdn/WebPath/EventDisp" \
             "$sdn/SeverityMaps/IntelligentAlarmSeverity" \
             "$pcause"/Prob067321[5-8]?_en_US "$evformat"/Event067321[5-8]?_en_US; do
        [ -e "$p" ] && files+=("$p")
    done

    local bad=()
    for p in ${dirs[@]+"${dirs[@]}"}; do
        { [ -w "$p" ] && [ -x "$p" ]; } || bad+=("$p")
    done
    for p in ${files[@]+"${files[@]}"}; do
        [ -w "$p" ] || bad+=("$p")
    done

    local home_q cmd_q
    home_q="$(printf '%q' "$SPECTRUM_HOME")"
    cmd_q="$(printf '%q ' "$0" "$@")"

    # Wrong identity: root creates root-owned files in a tree the Spectrum user must keep writing;
    # any other user who can't write needs to be the owner anyway.
    if [ "$me" != "$expected_user" ] && { [ "$me" = "root" ] || [ "${#bad[@]}" -gt 0 ]; }; then
        {
            echo "ERROR: run this as '$expected_user' (the owner of $ss_dir), not '$me'."
            echo "       Running as another user leaves files the Spectrum user can't write later."
            echo "Corrected command:"
            echo "  sudo -u $expected_user env SPECTRUM_HOME=$home_q $cmd_q"
        } >&2
        exit 1
    fi

    if [ "${#bad[@]}" -gt 0 ]; then
        {
            echo "ERROR: '$me' cannot write these Spectrum paths, so nothing was changed:"
            for p in "${bad[@]}"; do
                echo "  $(_mode_of "$p") $(_owner_of "$p"):$(_group_of "$p")  $p"
            done
            echo "Fix the ownership (as an admin with sudo), then re-run:"
            for p in "${bad[@]}"; do
                if [ "$(_owner_of "$p")" = "$me" ]; then
                    if [ -d "$p" ]; then echo "  sudo chmod u+rwx '$p'"; else echo "  sudo chmod u+rw '$p'"; fi
                else
                    echo "  sudo chown $expected_user:$expected_group '$p'"
                fi
            done
            echo "Re-run (as $me, without sudo):"
            echo "  SPECTRUM_HOME=$home_q $cmd_q"
        } >&2
        exit 1
    fi

    if [ "$me" != "$expected_user" ]; then
        echo "WARNING: running as '$me' but $ss_dir is owned by '$expected_user'; files this creates" >&2
        echo "         will be owned by '$me'." >&2
    fi
}

# preflight_oneclick_access ORIGINAL_ARGV...
# The OneClick host has no SS/ tree; it only needs CsPCause/CsEvFormat (and the marker) writable.
preflight_oneclick_access() {
    local sg="$SPECTRUM_HOME/SG-Support" p bad=()
    local me expected_user
    me="$(id -un)"
    if [ ! -d "$sg" ]; then
        echo "ERROR: $sg not found. Is SPECTRUM_HOME ($SPECTRUM_HOME) the Spectrum install dir?" >&2
        exit 1
    fi
    expected_user="$(_owner_of "$sg")"
    for p in "$sg/CsPCause" "$sg/CsEvFormat"; do
        if [ -d "$p" ]; then
            { [ -w "$p" ] && [ -x "$p" ]; } || bad+=("$p")
        fi
    done
    [ -d "$sg" ] && { [ -w "$sg" ] || bad+=("$sg"); }
    if [ "$me" != "$expected_user" ] && { [ "$me" = "root" ] || [ "${#bad[@]}" -gt 0 ]; }; then
        {
            echo "ERROR: run this as '$expected_user' (the owner of $sg), not '$me'."
            echo "Corrected command:"
            echo "  sudo -u $expected_user env SPECTRUM_HOME=$(printf '%q' "$SPECTRUM_HOME") $(printf '%q ' "$0" "$@")"
        } >&2
        exit 1
    fi
    if [ "${#bad[@]}" -gt 0 ]; then
        echo "ERROR: '$me' cannot write these paths, so nothing was changed:" >&2
        printf '  %s\n' "${bad[@]}" >&2
        exit 1
    fi
}
