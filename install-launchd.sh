#!/bin/sh
#
# Install the launchd agent, substituting this checkout's real path.
#
# Separate from copy-and-paste because launchd does not expand ~ or environment
# variables in ProgramArguments, so the template cannot be used as-is.

set -eu

CHECKOUT=$(cd "$(dirname "$0")" && pwd)
LABEL=com.github.stevelea.xpeng-mac-bridge
TEMPLATE="$CHECKOUT/launchd/$LABEL.plist.in"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -d "$CHECKOUT/XPENGBridge.app" ]; then
    echo "XPENGBridge.app is missing — run ./build-app.sh first." >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

sed -e "s|__CHECKOUT__|$CHECKOUT|g" -e "s|__HOME__|$HOME|g" "$TEMPLATE" > "$TARGET"
plutil -lint "$TARGET" >/dev/null

# Idempotent: bootout fails harmlessly when nothing is loaded.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

# launchd needs a moment to finish tearing the old job down. Bootstrapping in the
# same instant fails with "Bootstrap failed: 5: Input/output error", which reads
# like a broken plist and is not. Retry briefly.
attempt=1
while [ "$attempt" -le 5 ]; do
    if launchctl bootstrap "gui/$(id -u)" "$TARGET" 2>/dev/null; then
        break
    fi
    if [ "$attempt" -eq 5 ]; then
        echo "launchctl bootstrap kept failing; run it by hand to see the error:" >&2
        echo "  launchctl bootstrap gui/$(id -u) $TARGET" >&2
        exit 1
    fi
    sleep 2
    attempt=$((attempt + 1))
done

echo "installed $TARGET"
echo "logs:     $HOME/Library/Logs/xpeng-mac-bridge.log"
echo "stop:     launchctl bootout gui/$(id -u)/$LABEL"
