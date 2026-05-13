#!/usr/bin/env bash
# Build the EventKitCLI Swift binary required by the mcp-macos server.
#
# The mcp-macos npm package ships Swift sources but its postinstall hook
# often fails silently under npm/pnpm/npx, leaving the package "installed"
# without a working binary. The MCP server then exposes calendar/reminders
# tools that crash on first call with "EventKitCLI binary not found".
#
# This script finds the installed mcp-macos package, compiles the binary
# directly (no npm postinstall), and verifies the result.

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "ERROR: This script only runs on macOS." >&2
    exit 1
fi

if ! command -v swiftc >/dev/null 2>&1; then
    echo "ERROR: swiftc not found. Install Xcode Command Line Tools:" >&2
    echo "  xcode-select --install" >&2
    exit 1
fi

# Find the installed mcp-macos package
CANDIDATES=(
    "/opt/homebrew/lib/node_modules/mcp-macos"
    "/usr/local/lib/node_modules/mcp-macos"
    "$HOME/.npm/_npx/$(ls "$HOME/.npm/_npx/" 2>/dev/null | head -1)/node_modules/mcp-macos"
)

PKG_ROOT=""
for c in "${CANDIDATES[@]}"; do
    if [[ -d "$c" ]] && [[ -f "$c/package.json" ]]; then
        PKG_ROOT="$c"
        break
    fi
done

if [[ -z "$PKG_ROOT" ]]; then
    echo "ERROR: mcp-macos package not found. Install it first:" >&2
    echo "  npm install -g mcp-macos" >&2
    exit 1
fi

echo "Using mcp-macos at: $PKG_ROOT"

SRC_DIR="$PKG_ROOT/src/swift"
SOURCE_FILE="$SRC_DIR/EventKitCLI.swift"
INFO_PLIST="$SRC_DIR/Info.plist"
BIN_DIR="$PKG_ROOT/bin"
OUT_FILE="$BIN_DIR/EventKitCLI"

if [[ ! -f "$SOURCE_FILE" ]]; then
    echo "ERROR: Swift source missing: $SOURCE_FILE" >&2
    exit 1
fi
if [[ ! -f "$INFO_PLIST" ]]; then
    echo "ERROR: Info.plist missing: $INFO_PLIST" >&2
    echo "Info.plist is required for macOS to surface EventKit permission dialogs." >&2
    exit 1
fi

mkdir -p "$BIN_DIR"

# Detect compiler/SDK version mismatch up front (common after macOS updates)
SWIFT_VERSION="$(swiftc --version 2>/dev/null | head -1)"
echo "Swift: $SWIFT_VERSION"
echo "SDK:   $(xcrun --show-sdk-path 2>/dev/null || echo "<not found>")"

# Detect and offer to fix the macOS 26 SwiftBridging modulemap conflict before
# we try to compile (otherwise every build fails with redefinition + cascading
# 'this SDK is not supported by the compiler' noise).
STALE_MODMAP=/Library/Developer/CommandLineTools/usr/include/swift/module.modulemap
NEW_MODMAP=/Library/Developer/CommandLineTools/usr/include/swift/bridging.modulemap
if [[ -f "$STALE_MODMAP" ]] && [[ -f "$NEW_MODMAP" ]] \
   && grep -q "module SwiftBridging" "$STALE_MODMAP" 2>/dev/null \
   && grep -q "module SwiftBridging" "$NEW_MODMAP" 2>/dev/null; then
    echo "" >&2
    echo "DETECTED: macOS 26 SwiftBridging modulemap duplication." >&2
    echo "Both files define module 'SwiftBridging', which makes swiftc refuse" >&2
    echo "to compile anything that imports Foundation:" >&2
    echo "  $STALE_MODMAP   (older copy, must be moved aside)" >&2
    echo "  $NEW_MODMAP     (current copy, kept)" >&2
    echo "" >&2
    read -rp "Disable the older duplicate now (requires sudo)? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        sudo mv "$STALE_MODMAP" "${STALE_MODMAP}.disabled-by-ucs"
        echo "Renamed to ${STALE_MODMAP}.disabled-by-ucs" >&2
    else
        echo "Skipped. Build will likely fail." >&2
    fi
fi

echo "Compiling EventKitCLI (Info.plist embedded for TCC permissions)..."
BUILD_LOG="$(mktemp)"
trap 'rm -f "$BUILD_LOG"' EXIT

if swiftc \
    -o "$OUT_FILE" \
    "$SOURCE_FILE" \
    -framework EventKit \
    -framework Foundation \
    -Xlinker -sectcreate \
    -Xlinker __TEXT \
    -Xlinker __info_plist \
    -Xlinker "$INFO_PLIST" 2> "$BUILD_LOG"; then
    cat "$BUILD_LOG" >&2
else
    cat "$BUILD_LOG" >&2
    if grep -q "redefinition of module 'SwiftBridging'" "$BUILD_LOG"; then
        echo "" >&2
        echo "DIAGNOSIS: SwiftBridging modulemap conflict still present." >&2
        echo "Run this script again and answer 'y' when prompted, or manually:" >&2
        echo "  sudo mv $STALE_MODMAP ${STALE_MODMAP}.disabled-by-ucs" >&2
    elif grep -q "this SDK is not supported by the compiler" "$BUILD_LOG"; then
        echo "" >&2
        echo "DIAGNOSIS: Your Swift compiler and macOS SDK are out of sync." >&2
        echo "Fix with one of:" >&2
        echo "" >&2
        echo "  Option A (recommended): reinstall Command Line Tools" >&2
        echo "    sudo rm -rf /Library/Developer/CommandLineTools" >&2
        echo "    sudo xcode-select --install" >&2
        echo "" >&2
        echo "  Option B: if Xcode is installed, point at it" >&2
        echo "    sudo xcode-select -s /Applications/Xcode.app/Contents/Developer" >&2
        echo "" >&2
        echo "After fixing, re-run: bash ops/build_macos_swift.sh" >&2
    fi
    exit 1
fi

chmod 755 "$OUT_FILE"

# Verify
if [[ ! -x "$OUT_FILE" ]]; then
    echo "ERROR: Build claimed success but binary is not executable: $OUT_FILE" >&2
    exit 1
fi

echo ""
echo "Built: $OUT_FILE"
echo "Size: $(stat -f %z "$OUT_FILE") bytes"
echo ""
echo "Next: grant Calendar/Reminders permissions via ops/grant_permissions.sh"
