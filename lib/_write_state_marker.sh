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
EOF
    chmod 640 "$marker_path"
}
