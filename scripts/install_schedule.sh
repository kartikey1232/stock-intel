#!/bin/bash
# Install (or remove) the weekday 16:15 IST launchd job that runs scripts/scheduled_update.sh.
#
#   scripts/install_schedule.sh            install / reinstall
#   scripts/install_schedule.sh --remove   unload and delete the job
#
# The job is a per-user LaunchAgent (~/Library/LaunchAgents), so it runs as you, without a
# terminal open, whenever you're logged in to the Mac (screen locked is fine).
# StartCalendarInterval uses the Mac's local time; this assumes the system timezone is IST.

set -euo pipefail

LABEL="com.stockintel.update"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL"
fi

if [ "${1:-}" = "--remove" ]; then
    rm -f "$PLIST"
    echo "Removed $LABEL"
    exit 0
fi

tz="$(readlink /etc/localtime || true)"
case "$tz" in
    *Asia/Kolkata|*Asia/Calcutta) ;;
    *) echo "WARNING: system timezone is '$tz', not IST; the job runs at 16:15 local time." ;;
esac

chmod +x "$PROJECT_DIR/scripts/scheduled_update.sh"
mkdir -p "$PROJECT_DIR/logs" "$(dirname "$PLIST")"

# Monday (1) to Friday (5) at 16:15 local time.
intervals=""
for weekday in 1 2 3 4 5; do
    intervals+="
        <dict>
            <key>Weekday</key><integer>$weekday</integer>
            <key>Hour</key><integer>16</integer>
            <key>Minute</key><integer>15</integer>
        </dict>"
done

cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$PROJECT_DIR/scripts/scheduled_update.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>StartCalendarInterval</key>
    <array>$intervals
    </array>
    <!-- Only the wrapper's own startup errors land here; the run itself logs to
         logs/update-YYYY-MM-DD.log. -->
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/logs/launchd.err.log</string>
    <key>RunAtLoad</key>
    <false/>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST"
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed $LABEL: weekdays at 16:15. Plist: $PLIST"
