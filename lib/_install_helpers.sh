# lib/_install_helpers.sh
# Shared install_fragment/install_file helpers, extracted from the former spectrum-install/install.sh
# so both install-spectrum-vna.sh and install-spectrum-general.sh can source them.

# Appends $fragment to $target, but only if it's safe to do so:
#   - if $target already contains the fragment's exact byte content, skip (already installed)
#   - if $target contains none of the fragment's OID codes, append normally
#   - if $target contains some (but not an exact match) of the fragment's OID codes, that's
#     stale/conflicting content from an older generation of this tool -- refuse and bail out,
#     rather than attempting to auto-reconcile it.
install_fragment() {
    local fragment="$1" target="$2" label="$3"
    if [ -s "$target" ]; then
        local frag_content target_content
        frag_content="$(cat "$fragment")"
        target_content="$(cat "$target")"
        if [[ "$target_content" == *"$frag_content"* ]]; then
            echo "$label already contains the current appneta-ia fragment -- skipping"
            return
        fi
    fi

    local codes
    codes="$(grep -o '0x67321[0-9a-f][0-9a-f]' "$fragment" | sort -u)"

    local conflicts=()
    local code
    while IFS= read -r code; do
        [ -z "$code" ] && continue
        if grep -qF -- "$code" "$target" 2>/dev/null; then
            conflicts+=("$code")
        fi
    done <<< "$codes"

    if [ "${#conflicts[@]}" -gt 0 ]; then
        echo "ERROR: $target already contains conflicting/stale appneta-ia content." >&2
        echo "The following codes from $fragment are already present with different surrounding" >&2
        echo "content (a prior, different version of this tool's fragment, or hand-patched data):" >&2
        printf '  %s\n' "${conflicts[@]}" >&2
        echo "Manually reconcile $target before re-running this script." >&2
        exit 1
    fi

    echo "Appending $label to $target"
    cat "$fragment" >> "$target"
}

# Copies $src to $dest, but only if it's needed:
#   - if $dest exists and is byte-identical to $src, skip
#   - if $dest exists but differs, overwrite (an intentional content update)
#   - if $dest doesn't exist, install it
# $mode, if non-empty, is applied via chmod after install/update (not on skip).
install_file() {
    local src="$1" dest="$2" mode="${3:-}" name
    name="$(basename "$dest")"
    if [ -e "$dest" ] && cmp -s "$src" "$dest"; then
        echo "$name already installed and up to date -- skipping"
        return
    fi
    if [ -e "$dest" ]; then
        echo "$name exists but differs -- updating"
    fi
    cp "$src" "$dest"
    if [ -n "$mode" ]; then
        chmod "$mode" "$dest"
    fi
}
