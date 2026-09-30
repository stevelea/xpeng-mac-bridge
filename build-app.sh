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

# The executable is a compiled launcher, not a shell script. A shell script
# makes macOS run /bin/sh on it, and TCC then attributes the request to
# /bin/sh — so a Full Disk Access grant against this bundle is never consulted.
# See launcher.c, which carries the measured evidence.
if ! command -v clang >/dev/null 2>&1; then
    echo "clang not found. Install the Xcode Command Line Tools:" >&2
    echo "  xcode-select --install" >&2
    exit 1
fi

clang -O2 -Wall -Wextra -o "$APP/Contents/MacOS/XPENGBridge" \
    "$BUNDLE_DIR/launcher.c" 2>&1 | sed 's/^/  clang: /'

if [ ! -x "$APP/Contents/MacOS/XPENGBridge" ]; then
    echo "failed to build the launcher" >&2
    exit 1
fi

# Prove it is a real Mach-O rather than a script: this is the whole point.
if ! file "$APP/Contents/MacOS/XPENGBridge" | grep -q "Mach-O"; then
    echo "launcher is not a Mach-O binary; TCC would attribute it to its interpreter" >&2
    exit 1
fi

# Ad-hoc signature. Not notarised and not from an Apple developer account, but it
# gives the bundle a stable code identity, which is what the TCC grant is keyed
# to. Re-run this script after any edit or the grant stops applying.
codesign --force --sign - --identifier com.github.stevelea.xpeng-mac-bridge "$APP" 2>&1 |
    sed 's/^/  codesign: /'

echo "built $APP"
codesign --display --verbose=2 "$APP" 2>&1 | sed -n '1,6p' | sed 's/^/  /'
