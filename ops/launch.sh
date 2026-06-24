#!/usr/bin/env bash
# THE one launch path for Aria. Pins the running process to the trunk (main),
# refreshes the editable install so running == source, then execs the bot.
#
# Used by the launchd KeepAlive (ops/com.you.voicebot.plist), `make run`, and
# deploy.sh's restart — so there is exactly ONE way the bot starts and it always
# lands on the trunk. This is the structural fix for the drift that silently
# dropped tools from the live bot: an agent left HEAD on a feature branch and
# KeepAlive booted it. Now every boot returns to main first.
#
# It does NOT pkill anything (the launchd plist comment explains why: a
# `pkill -f src.bot` under KeepAlive can kill its own launching shell and
# crash-loop), and it `exec`s python so launchd supervises the bot process
# directly (PID preserved).
#
# If the tree is dirty or on a feature branch and checkout cannot switch
# cleanly, we still launch and let the CRITICAL `deployed_trunk` preflight gate
# refuse ready with the exact fix — loud and observable — rather than
# crash-loop launchd by aborting here.
set -uo pipefail

cd "$(dirname "$0")/.."  # repo root

if git checkout aria-live; then
    echo "launch: on trunk 'aria-live' (the live Discord Aria)"
else
    echo "launch: WARNING could not checkout 'aria-live' (dirty tree or conflict) —" \
         "the deployed_trunk preflight gate will refuse ready and print the fix"
fi

.venv/bin/pip install -e . --quiet
exec .venv/bin/python -u -m src.bot
