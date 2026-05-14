#!/usr/bin/env bash
# Kill Aria and all her sidecars. Launchd-aware: bootouts the service first
# so KeepAlive=true doesn't respawn the bot 5s after we kill it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

SELF=$$
PARENT=$PPID

# ---------------------------------------------------------------------------
# Phase 1: Bootout launchd if loaded
# ---------------------------------------------------------------------------
LABEL="com.you.voicebot"
DOMAIN="gui/$(id -u)"
PLIST="$SCRIPT_DIR/ops/com.you.voicebot.plist"
launchd_was_loaded=false

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo -e "${YELLOW}launchd:${RESET} $LABEL is loaded — booting out..."
    launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
    launchd_was_loaded=true
    sleep 1
fi

# ---------------------------------------------------------------------------
# Phase 2: Collect all PIDs in the process tree
# ---------------------------------------------------------------------------
collect_roots() {
    local -A seen=()
    for p in "$@"; do
        [[ -z "$p" ]] && continue
        [[ "$p" == "$SELF" || "$p" == "$PARENT" ]] && continue
        [[ -n "${seen[$p]:-}" ]] && continue
        kill -0 "$p" 2>/dev/null || continue
        seen[$p]=1
        echo "$p"
    done
}

expand_tree() {
    local root=$1
    echo "$root"
    for child in $(pgrep -P "$root" 2>/dev/null || true); do
        echo "$child"
        for gc in $(pgrep -P "$child" 2>/dev/null || true); do
            echo "$gc"
            for ggc in $(pgrep -P "$gc" 2>/dev/null || true); do
                echo "$ggc"
            done
        done
    done
}

ROOTS=()

while IFS= read -r p; do
    [[ -n "$p" ]] && ROOTS+=("$p")
done < <(pgrep -f "python.*src\.bot" 2>/dev/null || true)

while IFS= read -r p; do
    [[ -n "$p" ]] && ROOTS+=("$p")
done < <(pgrep -f "$SCRIPT_DIR/" 2>/dev/null || true)

while IFS= read -r p; do
    [[ -n "$p" ]] && ROOTS+=("$p")
done < <(pgrep -f "discord_voice_bridge/index\.js" 2>/dev/null || true)

while IFS= read -r p; do
    [[ -n "$p" ]] && ROOTS+=("$p")
done < <(pgrep -f "cursor_wrapper/index\.js" 2>/dev/null || true)

UNIQUE_ROOTS=()
while IFS= read -r p; do
    UNIQUE_ROOTS+=("$p")
done < <(collect_roots "${ROOTS[@]}")

ALL_PIDS=()
declare -A ALL_SEEN=()
for root in "${UNIQUE_ROOTS[@]}"; do
    while IFS= read -r p; do
        [[ "$p" == "$SELF" || "$p" == "$PARENT" ]] && continue
        if [[ -z "${ALL_SEEN[$p]:-}" ]]; then
            ALL_SEEN[$p]=1
            ALL_PIDS+=("$p")
        fi
    done < <(expand_tree "$root")
done

if [[ ${#ALL_PIDS[@]} -eq 0 ]]; then
    echo "No active Aria processes found."
    if [[ "$launchd_was_loaded" == true ]]; then
        echo ""
        echo -e "${DIM}launchd service was unloaded. To re-enable:${RESET}"
        echo "  launchctl bootstrap $DOMAIN $PLIST"
    fi
    exit 0
fi

echo -e "${BOLD}Found ${#ALL_PIDS[@]} process(es) to kill${RESET}"

# ---------------------------------------------------------------------------
# Phase 2b: SIGTERM children-before-parents, then SIGKILL survivors
# ---------------------------------------------------------------------------
killed=0
for (( i=${#ALL_PIDS[@]}-1; i>=0; i-- )); do
    p="${ALL_PIDS[$i]}"
    cmd=$(ps -p "$p" -o args= 2>/dev/null || echo "unknown")
    if kill "$p" 2>/dev/null; then
        echo -e "  ${DIM}TERM${RESET} $p — $cmd"
        killed=$((killed + 1))
    fi
done

sleep 1

survivors=0
for p in "${ALL_PIDS[@]}"; do
    if kill -0 "$p" 2>/dev/null; then
        kill -9 "$p" 2>/dev/null && {
            echo -e "  ${RED}KILL${RESET} $p (survived TERM)"
            survivors=$((survivors + 1))
        }
    fi
done

echo ""
echo -e "${GREEN}Done.${RESET} Killed $killed process(es)."
[[ $survivors -gt 0 ]] && echo -e "${YELLOW}$survivors required SIGKILL.${RESET}"

# ---------------------------------------------------------------------------
# Phase 3: Hint about launchd
# ---------------------------------------------------------------------------
if [[ "$launchd_was_loaded" == true ]]; then
    echo ""
    echo -e "${DIM}launchd service was unloaded. To re-enable:${RESET}"
    echo "  launchctl bootstrap $DOMAIN $PLIST"
fi
