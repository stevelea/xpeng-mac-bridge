#!/bin/sh
#
# Build XPENGBridge.app, the bundle you grant Full Disk Access to.
#
# Why a bundle exists at all: macOS TCC gates the XPENG app's container, so a
# launchd agent cannot read the bridge's data sources until it is granted Full
# Disk Access (see README). TCC identifies the requesting program by its code
# signature, and a bare "/usr/bin/python3 script.py" identifies as Python — so
# granting it would hand Full Disk Access to every Python script on the machine.
# Launching through a signed bundle makes the *bundle* the responsible process,
# scoping the grant to this one tool.
#
# Run this after moving the checkout, and any time these files change: the
# signature pins a hash, so a modified bundle loses the grant.

set -eu

BUNDLE_DIR=$(cd "$(dirname "$0")" && pwd)
APP="$BUNDLE_DIR/XPENGBridge.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>XPENGBridge</string>
    <key>CFBundleDisplayName</key>
    <string>XPENG Mac Bridge</string>
    <key>CFBundleIdentifier</key>
    <string>com.github.stevelea.xpeng-mac-bridge</string>
    <key>CFBundleExecutable</key>
    <string>XPENGBridge</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <!-- No UI: this is a background agent, not something to click. -->
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/XPENGBridge" <<'LAUNCHER'
#!/bin/sh
# Resolve the checkout from the bundle's own location so a moved repo still works:
# .../xpeng-mac-bridge/XPENGBridge.app/Contents/MacOS -> .../xpeng-mac-bridge
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
exec /usr/bin/python3 "$REPO/xpeng_bridge.py" "$@"
LAUNCHER

chmod +x "$APP/Contents/MacOS/XPENGBridge"

# Ad-hoc signature. Not notarised and not from an Apple developer account, but it
# gives the bundle a stable code identity, which is what the TCC grant is keyed
# to. Re-run this script after any edit or the grant stops applying.
codesign --force --sign - --identifier com.github.stevelea.xpeng-mac-bridge "$APP" 2>&1 |
    sed 's/^/  codesign: /'

echo "built $APP"
codesign --display --verbose=2 "$APP" 2>&1 | sed -n '1,6p' | sed 's/^/  /'
