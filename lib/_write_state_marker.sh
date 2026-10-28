# lib/_write_state_marker.sh
write_state_marker() {
    local mode="$1" marker_path="$2" version="$3" \
          alertmap_lines="$4" alertmap_sha="$5" eventdisp_lines="$6" eventdisp_sha="$7"
    cat > "$marker_path" <<EOF
mode=${mode}
version=${version}
installed=$(date -u +%Y-%m-%dT%H:%M:%SZ)
alertmap_lines_appended=${alertmap_lines}
alertmap_sha256=${alertmap_sha}
eventdisp_lines_appended=${eventdisp_lines}
eventdisp_sha256=${eventdisp_sha}
state=incomplete
EOF
    chmod 640 "$marker_path"
}

# The marker is written early so uninstall can always undo what was appended; state=incomplete says the
# installer had not finished. Call finish_state_marker as the very last step, after the MIB compile.
finish_state_marker() {
    local marker_path="$1" tmp rc=0
    tmp="$(mktemp)" || return 1
    { grep -v '^state=' "$marker_path" || true; echo "state=complete"; } > "$tmp" && cat "$tmp" > "$marker_path" || rc=1
    rm -f "$tmp"
    return $rc
}
