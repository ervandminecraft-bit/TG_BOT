#!/usr/bin/env bash
# Start the bot automatically when you log in to macOS (launchd), and restart it if it stops.
#   ./scripts/autostart_macos.sh          -> enable
#   ./scripts/autostart_macos.sh remove   -> disable
set -e
LABEL="com.subscriptionbot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$1" = "remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Автозапуск выключен / Autostart disabled"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT/logs"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$PROJECT/run.sh</string></array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PROJECT/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$PROJECT/logs/launchd.err.log</string>
</dict>
</plist>
PLISTEOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Автозапуск включён / Autostart enabled: $PLIST"
