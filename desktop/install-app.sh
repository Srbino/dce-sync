#!/bin/bash
# Build (or refresh) the "Outlands Discord.app" launcher on the Desktop.
#
#   ./desktop/install-app.sh                       # workspace auto-detected
#   ./desktop/install-app.sh --workspace DIR       # explicit
#   ./desktop/install-app.sh --dest ~/Applications # somewhere other than Desktop
#
# The bundle is a thin shim: it opens Terminal and runs desktop/export.command
# straight out of this repo. Nothing is copied in, so `git pull` updates the
# behaviour of the icon without reinstalling anything.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="Outlands Discord"
DEST="$HOME/Desktop"
WORKSPACE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --dest)      DEST="$2"; shift 2 ;;
    --name)      NAME="$2"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

# Default to the sibling checkout that holds the channel registry.
if [ -z "$WORKSPACE" ]; then
  for candidate in "$REPO/../uo-outlands-discord" "$PWD"; do
    if [ -f "$candidate/channels.yaml" ]; then
      WORKSPACE="$(cd "$candidate" && pwd)"
      break
    fi
  done
fi
[ -n "$WORKSPACE" ] || { echo "no channels.yaml found — pass --workspace DIR" >&2; exit 1; }
[ -f "$REPO/desktop/icon.icns" ] || { echo "missing desktop/icon.icns (run make-icon.mjs)" >&2; exit 1; }

APP="$DEST/$NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$REPO/desktop/icon.icns" "$APP/Contents/Resources/icon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleIdentifier</key><string>cz.srbino.outlands-discord.launcher</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

# The shim hands the real work to Terminal so the sync is visible and
# interruptible with Ctrl-C, rather than hidden in a background process.
cat > "$APP/Contents/MacOS/launcher" <<LAUNCHER
#!/bin/bash
REPO="$REPO"
WORKSPACE="$WORKSPACE"

if [ ! -d "\$REPO" ] || [ ! -d "\$WORKSPACE" ]; then
  osascript -e 'display alert "Outlands Discord" message "Nenašel jsem projekt ani složku s exporty.\n\nJe připojený disk SSD 990 PRO?" as critical'
  exit 1
fi

# Hold ⌥ while launching for a verbose run.
DEBUG=""
if osascript -e 'tell application "System Events" to (option key down) of (get properties)' 2>/dev/null | grep -qi true; then
  DEBUG=" --debug"
fi

CMD="'\$REPO/desktop/export.command' --workspace '\$WORKSPACE'\$DEBUG"

osascript <<OSA
tell application "Terminal"
  activate
  do script "\$CMD"
  delay 0.3
  try
    set number of columns of front window to 100
    set number of rows of front window to 42
    set custom title of front window to "Outlands Discord — export"
  end try
end tell
OSA
LAUNCHER

chmod +x "$APP/Contents/MacOS/launcher" "$REPO/desktop/export.command"
touch "$APP"   # nudge Finder into re-reading the icon

echo "✓ $APP"
echo "  repo:      $REPO"
echo "  workspace: $WORKSPACE"
