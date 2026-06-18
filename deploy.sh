#!/usr/bin/env bash
# Deploy the current code to the locally-running Aria bot.
# Shape: pre-deploy guards -> git push -> restart -> smoke test -> summary.
# Mirrors ../live_visuals_3/deploy.sh; adapted for ucs2's local-only model.
# Launchd persistence is managed separately and intentionally not touched here.
set -euo pipefail

cd "$(dirname "$0")"

if [ -f .env ]; then
    set -a; source .env; set +a
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

echo ""
echo -e "${BOLD}═══════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  deploy — Aria (ucs2)${RESET}"
echo -e "${BOLD}═══════════════════════════════════════════════════${RESET}"
echo ""

# ---------------------------------------------------------------------------
# Guard 0: on the trunk. The running process is pinned to main; deploying from
# a feature branch is the drift that silently dropped tools from the live bot
# (and `git push origin main` from a branch pushes main, not your work). Refuse
# loudly — land the work on main (merge) first, then deploy.
# ---------------------------------------------------------------------------
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "main" ]; then
    echo -e "${RED}\u2717 deploy refuses: on '$BRANCH', not the trunk 'main'. Merge to main first, then deploy.${RESET}"
    exit 1
fi

# ---------------------------------------------------------------------------
# Guard 1: REQUIRED env vars present in .env
# ---------------------------------------------------------------------------
echo -e "${CYAN}Pre-deploy guards...${RESET}"

.venv/bin/python3 - <<'PY'
import os, sys

REQUIRED_ENV = (
    "DISCORD_APP_BOT_TOKEN",
    "DISCORD_VOICE_BOT_TOKEN",
    "DISCORD_GUILD_ID",
    "DISCORD_VOICE_CHANNEL_ID",
    "DISCORD_TEXT_CHANNEL_ID",
    "DISCORD_LOG_CHANNEL_ID",
    "AUTHORIZED_USER_IDS",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
)

missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
if missing:
    for name in missing:
        print(f"\u2717 missing: {name}")
    sys.exit(1)
print(f"\u2713 all {len(REQUIRED_ENV)} required env vars present")
PY

# ---------------------------------------------------------------------------
# Guard 2: editable install (stale-code guard)
# ---------------------------------------------------------------------------
.venv/bin/pip install -e . --quiet
echo -e "  ${GREEN}\u2713${RESET} editable install up to date"

# ---------------------------------------------------------------------------
# Guard 3: preflight (skip cursor — bot isn't running to own the bridge)
# ---------------------------------------------------------------------------
if ! .venv/bin/python -m src.preflight --no-cursor --no-gemini; then
    echo -e "${RED}\u2717 preflight failed — fix before deploying${RESET}"
    exit 1
fi

# ---------------------------------------------------------------------------
# Guard 4: anchor pressure test (if ANCHOR_GATE_ENABLED=true)
# ---------------------------------------------------------------------------
if [[ "${ANCHOR_GATE_ENABLED:-false}" == "true" ]]; then
    echo -e "${CYAN}Running anchor pressure test...${RESET}"
    if ! .venv/bin/python tests/anchor_suite/run.py --gate --quick; then
        echo -e "${RED}\u2717 anchor suite failed — fix before deploying${RESET}"
        exit 1
    fi
    echo -e "  ${GREEN}\u2713${RESET} anchor suite passed"
fi

echo ""

# ---------------------------------------------------------------------------
# Git: commit and push
# ---------------------------------------------------------------------------
# .env exports GITHUB_TOKEN (a stale PAT used by other scripts). Git
# picks it up and uses it instead of the gh CLI's live OAuth token,
# causing "Invalid username or token" on every push. Unset it so git
# falls through to the credential helper chain (gh auth git-credential).
unset GITHUB_TOKEN GITHUB_TOKEN_MORE_SCOPE

echo -e "${CYAN}Git...${RESET}"
git add -A
git commit -m "${1:-deploy}" || echo "  Nothing to commit"
git push origin main
echo ""

# ---------------------------------------------------------------------------
# Restart: kill old, launch new
# ---------------------------------------------------------------------------
echo -e "${CYAN}Restarting Aria...${RESET}"
bash ./kill.sh

LOG="/tmp/ucs2-deploy-$(date +%s).log"
# Restart via the one launch path (pins to main + reinstall + exec), the same
# script the launchd KeepAlive uses, so deploy and KeepAlive can't diverge.
nohup bash ops/launch.sh > "$LOG" 2>&1 &
PID=$!
echo "  Launched src.bot (pid $PID)"
echo "  Log: $LOG"
echo ""

# ---------------------------------------------------------------------------
# Smoke test: poll the log for preflight result
# ---------------------------------------------------------------------------
echo -e "${CYAN}Waiting for preflight (up to 90s)...${RESET}"

smoke_ok=false
for i in $(seq 1 18); do
    sleep 5

    if ! kill -0 "$PID" 2>/dev/null; then
        echo -e "${RED}\u2717 src.bot exited early (check $LOG)${RESET}"
        tail -20 "$LOG" 2>/dev/null || true
        exit 1
    fi

    if grep -q "Preflight passed" "$LOG" 2>/dev/null; then
        echo -e "  ${GREEN}\u2713 Preflight passed${RESET}"
        smoke_ok=true
        break
    fi

    if grep -q "PREFLIGHT FAILED" "$LOG" 2>/dev/null; then
        echo -e "  ${RED}\u2717 Preflight FAILED${RESET}"
        grep "CRIT\|FAIL" "$LOG" 2>/dev/null | head -10
        echo -e "  ${DIM}Full log: $LOG${RESET}"
        exit 1
    fi

    echo -e "  ${DIM}attempt $i/18...${RESET}"
done

if [[ "$smoke_ok" != true ]]; then
    echo -e "${YELLOW}\u26a0 Timed out waiting for preflight. Bot may still be starting.${RESET}"
    echo -e "  ${DIM}Tail the log: tail -f $LOG${RESET}"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}═══════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  Aria is live${RESET}"
echo -e "  PID:    ${GREEN}$PID${RESET}"
echo -e "  Log:    ${DIM}$LOG${RESET}"
if [[ -n "${DISCORD_SERVER:-}" ]]; then
    echo -e "  Server: $DISCORD_SERVER"
fi
echo -e "${BOLD}═══════════════════════════════════════════════════${RESET}"
