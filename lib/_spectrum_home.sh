# lib/_spectrum_home.sh
# Sourced by intelligent_alarms.sh and the lib/*-spectrum*.sh scripts.
#
# resolve_spectrum_home leaves SPECTRUM_HOME set, or prints why not and exits 1.
# Order: an explicit SPECTRUM_HOME wins; otherwise SPECROOT from Spectrum's own
# spectrum80.env (the installer always leaves it in /opt/SPECTRUM, whatever install dir the
# site chose). If that file lives elsewhere, point SPECTRUM_ENV_FILE at it. There is no
# guessed default: a wrong tree is worse than stopping.

resolve_spectrum_home() {
    if [ -n "${SPECTRUM_HOME:-}" ]; then
        SPECTRUM_HOME="${SPECTRUM_HOME%/}"
        return 0
    fi

    local env_file="${SPECTRUM_ENV_FILE:-/opt/SPECTRUM/spectrum80.env}"
    local hint="Either export SPECTRUM_HOME=<Spectrum install dir> (e.g. /sw/Spectrum; Linux default is /usr/SPECTRUM per Broadcom),
or export SPECTRUM_ENV_FILE=<path to your spectrum80.env> (the file with the SPECROOT= line)."

    if [ ! -r "$env_file" ]; then
        printf 'ERROR: SPECTRUM_HOME is not set and %s was not found or is not readable.\n%s\n' "$env_file" "$hint" >&2
        exit 1
    fi

    local roots count
    roots="$(sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?SPECROOT=//p' "$env_file" | tr -d "\"'" | sed -E 's#/+$##' | sort -u)"
    count="$(printf '%s\n' "$roots" | grep -c .)" || true

    if [ "$count" -eq 0 ]; then
        printf 'ERROR: SPECTRUM_HOME is not set and %s has no SPECROOT= line.\n%s\n' "$env_file" "$hint" >&2
        exit 1
    fi
    if [ "$count" -gt 1 ]; then
        printf 'ERROR: %s has conflicting SPECROOT= values, refusing to guess:\n%s\n' "$env_file" "$roots" >&2
        echo "Export SPECTRUM_HOME explicitly to choose one." >&2
        exit 1
    fi
    if [ ! -d "$roots" ]; then
        printf 'ERROR: SPECROOT=%s from %s is not a directory.\n%s\n' "$roots" "$env_file" "$hint" >&2
        exit 1
    fi

    SPECTRUM_HOME="$roots"
    echo "Using SPECTRUM_HOME=$SPECTRUM_HOME (SPECROOT from $env_file)" >&2
}
