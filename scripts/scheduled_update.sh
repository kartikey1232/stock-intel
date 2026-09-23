#!/bin/bash
# Scheduled entry point for run_update.py (called by launchd; see scripts/install_schedule.sh).
#
# launchd starts jobs with a minimal environment: no login shell, PATH=/usr/bin:/bin:...,
# working directory "/". So this script uses absolute paths, cds into the project, and
# logs everything to logs/update-YYYY-MM-DD.log (IST date).

set -u

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UV="${UV:-$HOME/.local/bin/uv}"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/update-$(TZ=Asia/Kolkata date +%F).log"
KEEP_DAYS=60

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

mkdir -p "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

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

"$UV" run python run_update.py
status=$?
echo "=== $(TZ=Asia/Kolkata date '+%F %T %Z') scheduled update finished with exit code $status ==="

# Keep a couple of months of dated logs.
find "$LOG_DIR" -name 'update-*.log' -type f -mtime +"$KEEP_DAYS" -delete

exit "$status"
