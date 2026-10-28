# lib/_logging.sh
# start_logging <target> <action>: from here on, copy everything the script and its child installers print
# (stdout and stderr, still on their own streams) into <yyyymmddhhmm>-<action>.log under a logs directory,
# created if missing. Default: <repo>/logs; override with IA_LOG_DIR. If the directory can't be
# written, warn and carry on without a log rather than blocking an install.
start_logging() {
    local label="$1-$2" dir="${IA_LOG_DIR:-$SCRIPT_DIR/logs}"
    if ! (umask 027; mkdir -p -m 750 "$dir") 2>/dev/null || [ ! -w "$dir" ]; then
        echo "WARNING: cannot write log directory $dir -- continuing without a log." >&2
        return 0
    fi
    LOG_FILE="$dir/$(date +%Y%m%d%H%M)-$2.log"
    # The name is predictable, so never follow a symlink planted at it (CWE-59). New files get 640
    # from the umask at creation; an existing file is appended to and its mode left alone. Two
    # same-action runs in one minute share a file, each starting with its own header.
    if [ -L "$LOG_FILE" ] || ! (umask 027; touch "$LOG_FILE") 2>/dev/null || [ ! -w "$LOG_FILE" ]; then
        echo "WARNING: cannot safely write log file $LOG_FILE -- continuing without a log." >&2
        return 0
    fi

    # $! after a process substitution is only set on bash 4.4+; under set -u an unset $! would abort the
    # install with no message, so default it (and _finish_logging then waits without a pid).
    exec > >(tee -a "$LOG_FILE")
    LOG_OUT_PID="${!:-}"
    exec 2> >(tee -a "$LOG_FILE" >&2)
    LOG_ERR_PID="${!:-}"
    trap '_finish_logging $?' EXIT

    echo "=== intelligent_alarms.sh $label: $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
    echo "user=$(id -un) host=$(hostname) cwd=$PWD"
    echo "args: ${ORIG_ARGS[*]}"
    echo "log:  $LOG_FILE"
}

# Print the result, then close our pipes and wait for the tees so no output lands after the prompt.
_finish_logging() {
    echo "=== exit status $1 -- log: $LOG_FILE ==="
    exec >&- 2>&-
    wait ${LOG_OUT_PID:+"$LOG_OUT_PID"} ${LOG_ERR_PID:+"$LOG_ERR_PID"} 2>/dev/null || true
}
