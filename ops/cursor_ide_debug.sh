#!/usr/bin/env bash
# Enable Aria's real IDE actuator: relaunch Cursor with a CDP control port.
#
# WHY: Aria drives the Cursor IDE for real over the Chrome DevTools Protocol
# (src/cursor_ide_driver.py) — focus the chat composer, insert text as a
# trusted input, press Enter as a trusted key, and confirm the send by the
# transcript actually advancing. Cursor only exposes CDP when it is launched
# with `--remote-debugging-port`, so this script relaunches it with that flag.
# Until you run this, IDE sends return an honest "CDP not enabled" blocker —
# never a fake "delivered".
#
# COST: this QUITS and RELAUNCHES Cursor once. Your windows reopen on the same
# (signed-in) profile. Run it when you're at a stopping point. It is the user's
# one-time enablement — Aria never runs it for you (it would kill her own host
# session). Make it permanent by always launching Cursor this way.
#
# USAGE:  bash ops/cursor_ide_debug.sh [PORT] [--yes]
#         PORT defaults to $CURSOR_CDP_PORT or 9223 (must match config.py).
set -euo pipefail

PORT="${1:-${CURSOR_CDP_PORT:-9223}}"
if [[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]]; then PORT="${CURSOR_CDP_PORT:-9223}"; fi
ASSUME_YES="no"
for a in "$@"; do [[ "$a" == "--yes" || "$a" == "-y" ]] && ASSUME_YES="yes"; done

if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
  echo "error: PORT must be a number (got '$PORT')" >&2
  exit 2
fi

CURSOR_BIN="/Applications/Cursor.app/Contents/MacOS/Cursor"
if [[ ! -x "$CURSOR_BIN" ]]; then
  echo "error: Cursor not found at $CURSOR_BIN" >&2
  exit 1
fi

echo "This will QUIT and RELAUNCH Cursor with --remote-debugging-port=$PORT."
echo "All Cursor windows will close and reopen (same profile)."
if [[ "$ASSUME_YES" != "yes" ]]; then
  read -r -p "Proceed? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "aborted."; exit 0; }
fi

echo "quitting Cursor…"
osascript -e 'tell application "Cursor" to quit' >/dev/null 2>&1 || true
for _ in $(seq 1 20); do
  pgrep -x "Cursor" >/dev/null 2>&1 || break
  sleep 0.5
done
pgrep -x "Cursor" >/dev/null 2>&1 && { echo "force-quitting…"; pkill -x "Cursor" 2>/dev/null || true; sleep 1; }

echo "relaunching Cursor with CDP port $PORT…"
open -a "Cursor" --args --remote-debugging-port="$PORT"
sleep 4

if curl -s --max-time 4 "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
  echo "OK: Cursor is up and the CDP port $PORT is live. Aria can now drive the IDE."
else
  echo "NOTE: relaunched, but the CDP port $PORT did not answer yet."
  echo "      Give Cursor a few seconds, then check: curl http://127.0.0.1:${PORT}/json/version"
fi
