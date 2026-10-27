#!/bin/bash
# Removes EasySwitch Relay for the current macOS user.
#
#     bash uninstall.sh                # keep settings and log
#     bash uninstall.sh --remove-logs  # remove those too
#
# Also removes its Input Monitoring permission entry. After this, the
# Easy-Switch keys go back to switching only the keyboard.
set -uo pipefail
LABEL="local.easyswitch.relay"
APP="$HOME/Applications/EasySwitch Relay.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/EasySwitchRelay"
DATA_DIR="$HOME/Library/Application Support/EasySwitchRelay"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
pkill -f "EasySwitch Relay.app/Contents/MacOS/easyswitch-relay" 2>/dev/null
rm -f "$PLIST"
rm -rf "$APP"
tccutil reset ListenEvent "$LABEL" >/dev/null 2>&1
if [[ "${1:-}" == "--remove-logs" ]]; then rm -rf "$LOG_DIR" "$DATA_DIR"; fi

echo "EasySwitch Relay removed."
[[ "${1:-}" == "--remove-logs" ]] || echo "Settings and log kept (use --remove-logs to delete them)."
