#!/bin/bash
# Installs (or updates) EasySwitch Relay for the current macOS user.
#
#     bash install.sh            install, start at login, start now, check devices
#     bash install.sh --check    only re-run the device/permission check
#
# No admin password needed. The app goes in ~/Applications and a login agent
# in ~/Library/LaunchAgents keeps the relay running (and restarts it if it
# crashes). Opening the app (double-click) shows the settings window. Your
# settings are kept when you update.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="EasySwitch Relay.app"
DEST_DIR="$HOME/Applications"
APP="$DEST_DIR/$APP_NAME"
LABEL="local.easyswitch.relay"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/EasySwitchRelay"
LOG="$LOG_DIR/relay.log"
DOMAIN="gui/$(id -u)"

check() {
    echo "Checking your keyboard and mouse (they need to be connected to this Mac)."
    echo "Move the mouse and press a key now so both are awake (this takes up to 10 seconds)..."
    mkdir -p "$LOG_DIR"; touch "$LOG"
    local before; before=$(wc -l < "$LOG")
    # Launched through 'open' so macOS treats the app itself (not Terminal) as
    # the program asking for Input Monitoring.
    open -W -n "$APP" --args --probe
    tail -n +"$((before + 1))" "$LOG" | sed 's/^/  /'
    if tail -n +"$((before + 1))" "$LOG" | grep -q "RESULT: OK"; then
        return 0
    fi
    echo
    echo "If it found nothing: open System Settings > Privacy & Security > Input Monitoring,"
    echo "turn on 'EasySwitch Relay', then run:  bash \"$SRC/install.sh\" --check"
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent" || true
    return 1
}

if [[ "${1:-}" == "--check" ]]; then check; exit $?; fi

if [[ ! -d "$SRC/$APP_NAME" ]]; then
    echo "'$APP_NAME' not found next to this script. Run install.sh from the built folder." >&2
    exit 1
fi

# Stop a running copy.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
pkill -f "$APP_NAME/Contents/MacOS/easyswitch-relay" 2>/dev/null || true
sleep 0.5

# Install the app.
mkdir -p "$DEST_DIR"
rm -rf "$APP"
ditto "$SRC/$APP_NAME" "$APP"
# A zip that came from a download/AirDrop marks the app as quarantined, which
# makes Gatekeeper refuse an unnotarized app. You chose to install it, so clear it.
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

# Login agent: start at login, restart after a crash, normal (not background)
# scheduling priority so key presses are handled without delay.
mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>             <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$APP/Contents/MacOS/easyswitch-relay</string>
        <string>--run</string>
    </array>
    <key>RunAtLoad</key>         <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key> <false/>
    </dict>
    <key>ThrottleInterval</key>  <integer>10</integer>
    <key>ProcessType</key>       <string>Interactive</string>
    <key>StandardErrorPath</key> <string>$LOG_DIR/stderr.log</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null

# Check first (this is also when macOS asks for Input Monitoring), then start.
echo
check || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo
echo "EasySwitch Relay is installed and running. Opening the settings window;"
echo "you can reopen it any time by opening 'EasySwitch Relay' in your Applications folder."
echo "If macOS asks whether EasySwitch Relay may accept incoming network connections,"
echo "choose Allow (needed for network follow)."
echo "Log: $LOG"
sleep 1
open "$APP"
