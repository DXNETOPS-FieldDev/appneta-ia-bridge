# lib/_compile_mibs.sh
# compile_mibs <mib-dir>: compile every .mib in <mib-dir> with Spectrum's BulkMibCompile.sh.
# Skips (return 0) when this host has no MibDatabase, i.e. the SpectroSERVER-only host of a split
# install; the compile belongs on the OneClick host. BulkMibCompile.sh exits 0 even when a MIB
# fails to parse, so success is judged from its output, not its return code.
compile_mibs() {
    local mib_dir="$1"
    local compiler="$SPECTRUM_HOME/MibDatabase/scripts/BulkMibCompile.sh"
    if [ ! -f "$compiler" ]; then
        echo "No MibDatabase on this host -- skipping the MIB compile. On a split install, run the"
        echo "OneClick host's --oneclick install: the compile belongs there."
        return 0
    fi
    if [ ! -r "$compiler" ]; then
        echo "ERROR: cannot read $compiler as $(id -un); run this as the Spectrum user." >&2
        return 1
    fi

    # Callers run this as `compile_mibs ... || exit 1`, which turns errexit off in here, so every
    # step that must not be ignored is checked by hand.
    local scratch out rc=0
    compgen -G "$mib_dir/*.mib" >/dev/null || { echo "ERROR: no .mib files in $mib_dir" >&2; return 1; }
    scratch="$(mktemp -d)" || { echo "ERROR: cannot create a scratch directory (is /tmp writable?)" >&2; return 1; }
    cp "$mib_dir"/*.mib "$scratch"/ || { rm -rf "$scratch"; echo "ERROR: cannot copy the MIBs to $scratch" >&2; return 1; }
    echo "Compiling MIBs: $(cd "$scratch" && ls *.mib | tr '\n' ' ')"
    out="$(bash "$compiler" -d "$scratch" -f "*.mib" 2>&1)" || rc=$?
    rm -rf "$scratch"

    if [ "$rc" -ne 0 ] || grep -q 'MIB failed to compile' <<<"$out" \
            || ! grep -q 'Compiling : APPNETA-INTELLIGENT-ALARM-MIB.mib' <<<"$out"; then
        echo "$out" >&2
        echo "ERROR: MIB compile failed (see output above). The files are installed; fix the" >&2
        echo "problem and re-run this command, or compile $mib_dir/*.mib by hand." >&2
        return 1
    fi
    echo "MIBs compiled. Restart MIB Tools clients to see them."
}
