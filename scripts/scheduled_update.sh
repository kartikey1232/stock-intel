#!/bin/bash
# Scheduled entry point for run_update.py (called by launchd; see scripts/install_schedule.sh).
#
# launchd starts jobs with a minimal environment: no login shell, PATH=/usr/bin:/bin:...,
# working directory "/". So this script uses absolute paths, cds into the project, and
# logs everything to logs/update-YYYY-MM-DD.log (IST date).
#
# run_update.py sends failures to Telegram. On a non-zero exit this script shows a macOS
# notification naming the failed steps and the log file, but only as a fallback: if the
# failures file says Telegram delivered, it stays silent. On success it stays silent.
# UV and LOG_DIR can be overridden, e.g. to simulate a failure with a fake uv.

set -u

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UV="${UV:-$HOME/.local/bin/uv}"
LOG_DIR="${LOG_DIR:-$PROJECT_DIR/logs}"
LOG_FILE="$LOG_DIR/update-$(TZ=Asia/Kolkata date +%F).log"
KEEP_DAYS=60
FAILURES_FILE="$(mktemp -t stockintel-failures)"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

mkdir -p "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

# Show a notification for a failed run: $1 = exit code. Text is passed as osascript
# arguments, so quotes in step names need no escaping.
notify_failure() {
    local status="$1" failed summary
    failed="$(grep -v '^#' "$FAILURES_FILE" 2>/dev/null | paste -sd ',' - | sed 's/,/, /g')"
    if [ -n "$failed" ]; then
        summary="Failed: $failed"
    else
        summary="Exit code $status before the pipeline finished"
    fi
    [ "${#summary}" -gt 180 ] && summary="${summary:0:177}..."
    /usr/bin/osascript \
        -e 'on run argv' \
        -e 'display notification (item 1 of argv) with title "stock-intel update failed" subtitle (item 2 of argv)' \
        -e 'end run' \
        "$summary" "Log: ${LOG_FILE#"$PROJECT_DIR"/}" \
        || echo "WARNING: could not show the failure notification"
}

on_exit() {
    local status=$?
    # run_update.py sends failures to Telegram; the notification is only a fallback for
    # when that didn't happen (Telegram not configured, down, or the run crashed).
    if [ "$status" -ne 0 ] && ! grep -qx '# telegram: delivered' "$FAILURES_FILE" 2>/dev/null; then
        notify_failure "$status"
    fi
    rm -f "$FAILURES_FILE"
}
trap on_exit EXIT

echo "=== $(TZ=Asia/Kolkata date '+%F %T %Z') scheduled update starting (pid $$) ==="

if ! cd "$PROJECT_DIR"; then
    echo "ERROR: cannot cd to $PROJECT_DIR"
    exit 1
fi
if [ ! -x "$UV" ]; then
    echo "ERROR: uv not found at $UV"
    exit 1
fi

# After waking from sleep the network can take a minute to come back; wait up to 5 min.
for attempt in $(seq 1 30); do
    if /usr/bin/curl -s -o /dev/null --max-time 5 https://query1.finance.yahoo.com; then
        break
    fi
    [ "$attempt" -eq 30 ] && echo "WARNING: network still unreachable after 5 min; running anyway"
    sleep 10
done

"$UV" run python run_update.py --failures-file "$FAILURES_FILE"
status=$?
echo "=== $(TZ=Asia/Kolkata date '+%F %T %Z') scheduled update finished with exit code $status ==="

# Keep a couple of months of dated logs.
find "$LOG_DIR" -name 'update-*.log' -type f -mtime +"$KEEP_DAYS" -delete

exit "$status"
