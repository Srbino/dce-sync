#!/bin/bash
# Build (or refresh) the "Discord Archive.app" launcher on the Desktop.
#
#   ./desktop/install-app.sh                       # workspace auto-detected
#   ./desktop/install-app.sh --workspace DIR       # explicit
#   ./desktop/install-app.sh --dest ~/Applications # somewhere other than Desktop
#
# The bundle is a thin shim: it opens the local browser dashboard
# straight out of this repo. Nothing is copied in, so `git pull` updates the
# behaviour of the icon without reinstalling anything.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="Discord Archive"
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

# Use the current directory or checkout when no workspace is specified.
if [ -z "$WORKSPACE" ]; then
  for candidate in "$PWD" "$REPO"; do
    if [ -f "$candidate/channels.yaml" ]; then
      WORKSPACE="$(cd "$candidate" && pwd)"
      break
    fi
  done
fi
[ -n "$WORKSPACE" ] || { echo "no channels.yaml found — pass --workspace DIR" >&2; exit 1; }
[ -f "$WORKSPACE/channels.yaml" ] || { echo "no channels.yaml in workspace" >&2; exit 1; }
WORKSPACE="$(cd "$WORKSPACE" && pwd)"
printf -v REPO_LITERAL '%q' "$REPO"
printf -v WORKSPACE_LITERAL '%q' "$WORKSPACE"
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
  <key>CFBundleIdentifier</key><string>io.github.srbino.discord-archive</string>
  <key>CFBundleVersion</key><string>0.3.0</string>
  <key>CFBundleShortVersionString</key><string>0.3.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

# Open the local dashboard without a Terminal window.
cat > "$APP/Contents/MacOS/launcher" <<LAUNCHER
#!/bin/bash
export PATH="\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:\$PATH"
REPO=$REPO_LITERAL
WORKSPACE=$WORKSPACE_LITERAL
if [ ! -d "\$REPO" ] || [ ! -f "\$WORKSPACE/channels.yaml" ]; then
  osascript -e 'display alert "Discord Archive" message "Project or channels.yaml not found. Is the archive drive connected?" as critical'
  exit 1
fi
PYTHON="\$REPO/.venv/bin/python"
[ -x "\$PYTHON" ] || PYTHON=python3
mkdir -p "\$HOME/Library/Logs/dce-sync"
cd "\$WORKSPACE" || exit 1
nohup "\$PYTHON" "\$REPO/dce" --config "\$WORKSPACE/channels.yaml" app >> "\$HOME/Library/Logs/dce-sync/app.log" 2>&1 &
LAUNCHER

chmod +x "$APP/Contents/MacOS/launcher" "$REPO/desktop/export.command"
touch "$APP"   # nudge Finder into re-reading the icon

echo "✓ $APP"
echo "  repo:      $REPO"
echo "  workspace: $WORKSPACE"
