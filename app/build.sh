#!/usr/bin/env bash
# Build katet.app and install it to ~/Applications. The app opens any workspace at runtime
# (Workspace menu, a folder dropped on its icon, `open -a katet DIR`); only the framework path
# is baked into Info.plist.
#   katet app build [--dest DIR] [--id BUNDLE_ID]
set -euo pipefail
CALLER="$PWD"
cd "$(dirname "$0")"
FRAMEWORK="$(cd .. && pwd)"
DEST="$HOME/Applications"; APP_ID="local.katet"
while (($#)); do
  case "$1" in
    --dest) [[ $# -ge 2 ]] || { echo "--dest needs a directory" >&2; exit 1; }; DEST="$2"; shift 2 ;;
    --id) [[ $# -ge 2 ]] || { echo "--id needs a bundle id" >&2; exit 1; }; APP_ID="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 1 ;;
  esac
done
[[ "$DEST" = /* ]] || DEST="$CALLER/$DEST"     # a relative --dest is relative to where you ran it
xml() { printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }
B=build/katet.app
rm -rf build && mkdir -p "$B/Contents/MacOS" "$B/Contents/Resources"

echo "· compiling"
swiftc -O -swift-version 5 main.swift -o "$B/Contents/MacOS/katet" \
  -framework Cocoa -framework WebKit -framework UserNotifications

echo "· icon + menu-bar images (assets/brand is the source; app/icon holds the built files)"
cp icon/AppIcon.icns "$B/Contents/Resources/katet.icns"
cp icon/MenuBar*Template*.png "$B/Contents/Resources/"

cat > "$B/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>katet</string>
  <key>CFBundleDisplayName</key><string>katet</string>
  <key>CFBundleIdentifier</key><string>$(xml "$APP_ID")</string>
  <key>CFBundleExecutable</key><string>katet</string>
  <key>CFBundleIconFile</key><string>katet</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>$(date +%Y%m%d%H%M)</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>KatetFramework</key><string>$(xml "$FRAMEWORK")</string>
  <key>CFBundleDocumentTypes</key><array><dict>
    <key>CFBundleTypeName</key><string>katet workspace</string>
    <key>CFBundleTypeRole</key><string>Viewer</string>
    <key>LSHandlerRank</key><string>Alternate</string>
    <key>LSItemContentTypes</key><array><string>public.folder</string></array>
  </dict></array>
</dict></plist>
PLIST

echo "· signing (ad hoc, local use)"
codesign --force --deep --sign - "$B" >/dev/null

# a running copy of this very bundle would keep the old code (and `open` would just activate it):
# quit it, relaunch after. Other copies of katet.app elsewhere are left alone.
running=
BIN="$DEST/katet.app/Contents/MacOS/katet"
if pgrep -fq "^$BIN"; then
  running=1
  echo "· quitting the running app"
  pkill -TERM -f "^$BIN" || true    # the app turns SIGTERM into a normal quit: window state and unsent text are kept
  for _ in $(seq 80); do pgrep -fq "^$BIN" || break; sleep 0.25; done
  if pgrep -fq "^$BIN"; then echo "the running app did not quit — quit it and run the build again" >&2; exit 1; fi
fi
mkdir -p "$DEST"
rm -rf "$DEST/katet.app"
cp -R "$B" "$DEST/"
# only the installed copy may be known to LaunchServices: otherwise `open -a katet` can pick the
# build folder's bundle, and notifications and preferences end up on the wrong app
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
"$LSREG" -u "$PWD/$B" >/dev/null 2>&1 || true
"$LSREG" -f "$DEST/katet.app" >/dev/null 2>&1 || true
echo "installed: $DEST/katet.app  (id: $APP_ID)"
if [[ -n "$running" ]]; then
  echo "· relaunching"
  open "$DEST/katet.app"
fi
