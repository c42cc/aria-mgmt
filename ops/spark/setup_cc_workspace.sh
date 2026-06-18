#!/usr/bin/env bash
#
# setup_cc_workspace.sh — stand up (or update) the live_visuals_4 Claude Code
# workspace on a DGX Spark node, at parity with the Mac's live_visuals_4_CC
# environment. Runs FROM the Mac; the bootstrap step runs ON the node.
#
# What it does (idempotent — safe to re-run; this is the "just update it" path):
#   1. rsync live_visuals_4 (working tree + .git) -> spark:~/live_visuals_4,
#      excluding the committed control-plane overlay and the heavy regenerable
#      runtime (.venv, node_modules, data/*, archives). Additive by default so a
#      collapse branch already on the node is preserved; pass --mirror for a
#      pristine re-mirror (adds rsync --delete).
#   2. Overlay the Claude Code control-plane from ops/spark/cc_workspace/
#      (.claude/, .mcp.json, CLAUDE.md, scripts/bootstrap_cc.sh).
#   3. git-exclude the overlay (so it never lands in a collapse commit) and set a
#      repo-local commit identity (the node has no global git identity).
#   4. Run scripts/bootstrap_cc.sh on the node (rebuilds venvs + node_modules).
#
# Auth is NOT done here: Claude Code's Max-subscription login is a one-time
# user action on the node (`ssh -t <node>` then `claude` -> `/login`). This
# script never sets ANTHROPIC_API_KEY (that would shadow the subscription).
#
# Usage:
#   ops/spark/setup_cc_workspace.sh [node] [--mirror]
#   SKIP_BOOTSTRAP=1 ops/spark/setup_cc_workspace.sh spark1   # sync+overlay only

set -euo pipefail

NODE="${1:-spark1}"
MODE="${2:-}"

HERE="$(cd "$(dirname "$0")" && pwd)"        # ucs2/ops/spark
UCS2_ROOT="$(cd "$HERE/../.." && pwd)"       # ucs2
AGI_ENV="$(cd "$UCS2_ROOT/.." && pwd)"       # agi_env_v1
LV4_LOCAL="$AGI_ENV/live_visuals_4"
TEMPLATE="$HERE/cc_workspace"
REMOTE_DIR="live_visuals_4"                  # relative to the node user's $HOME

log() { printf '[setup-cc %s] %s\n' "$NODE" "$*"; }
die() { printf '[setup-cc %s] FATAL: %s\n' "$NODE" "$*" >&2; exit 1; }

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15)

# ── Pre-flight ───────────────────────────────────────────────────────────────
[ -d "$LV4_LOCAL/.git" ] || die "live_visuals_4 git checkout not found at $LV4_LOCAL"
[ -d "$TEMPLATE/.claude" ] || die "control-plane template missing at $TEMPLATE"
"${SSH[@]}" -n "$NODE" true 2>/dev/null || die "cannot reach $NODE over SSH (is Tailscale up / re-authed?)"
log "local repo: $LV4_LOCAL"

DEL=()
if [ "$MODE" = "--mirror" ]; then
  DEL=(--delete)
  log "MIRROR mode: rsync --delete (pristine re-mirror — any node-only branch state will be removed)"
fi

# ── 1. Sync the repo (tree + .git), minus overlay + regenerable runtime ──────
log "rsync -> $NODE:~/$REMOTE_DIR  (first run copies .git; later runs are incremental)"
rsync -az "${DEL[@]}" -e "ssh -o BatchMode=yes" \
  --exclude='.claude/' \
  --exclude='.mcp.json' \
  --exclude='/CLAUDE.md' \
  --exclude='scripts/bootstrap_cc.sh' \
  --exclude='.venv/' \
  --exclude='node_modules/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  --exclude='*.log' \
  --exclude='/data/sessions/' \
  --exclude='/data/archive/' \
  --exclude='/data/TODAY/' \
  --exclude='/data/world/' \
  --exclude='/data/recording_finalized/' \
  --exclude='*.zip' \
  "$LV4_LOCAL/" "$NODE:$REMOTE_DIR/" \
  || die "rsync of the repo failed"
log "repo synced"

# ── 2. Overlay the Claude Code control-plane ─────────────────────────────────
log "overlay control-plane (.claude/, .mcp.json, CLAUDE.md, bootstrap)"
rsync -az -e "ssh -o BatchMode=yes" "$TEMPLATE/" "$NODE:$REMOTE_DIR/" \
  || die "overlay rsync failed"

# ── 3. git-exclude the overlay + set a commit identity ───────────────────────
"${SSH[@]}" -n "$NODE" "
  set -e
  cd ~/$REMOTE_DIR
  touch .git/info/exclude
  for p in '.claude/' '.mcp.json' '/CLAUDE.md' 'scripts/bootstrap_cc.sh'; do
    grep -qxF \"\$p\" .git/info/exclude || echo \"\$p\" >> .git/info/exclude
  done
  git config user.name 'Aria ($NODE)'
  git config user.email 'aria@$NODE.local'
  chmod +x scripts/bootstrap_cc.sh 2>/dev/null || true
  # Interactive claude on this node defaults to MAX effort (model + thinking come
  # from the committed .claude/settings.json: Opus 4.8, thinking on). The audit
  # runner is NON-interactive (never sources ~/.bashrc) and pins its own medium
  # effort + no extended thinking, so this default never bleeds into the run.
  grep -qxF 'export CLAUDE_CODE_EFFORT_LEVEL=max' ~/.bashrc 2>/dev/null || echo 'export CLAUDE_CODE_EFFORT_LEVEL=max' >> ~/.bashrc
  echo \"  HEAD: \$(git rev-parse --short HEAD) on \$(git branch --show-current)\"
" || die "post-sync git config failed"
log "overlay git-excluded; commit identity set; interactive claude default = Opus 4.8 @ max effort"

# ── 4. Bootstrap (venvs + node_modules) ──────────────────────────────────────
if [ "${SKIP_BOOTSTRAP:-}" = "1" ]; then
  log "SKIP_BOOTSTRAP=1 — skipping venv/node_modules rebuild"
else
  log "bootstrap on $NODE (uv venv + npm ci x3 + verify) — this can take several minutes"
  # Source env.sh so ~/.local/bin (node/npm/uv/claude) is on PATH for the
  # non-interactive shell. NEVER source .env (would shadow the subscription).
  "${SSH[@]}" -n "$NODE" "source ~/.config/spark/env.sh 2>/dev/null; cd ~/$REMOTE_DIR && bash scripts/bootstrap_cc.sh" \
    || die "bootstrap_cc.sh failed on $NODE"
  log "bootstrap complete"
fi

# ── 5. Optional: smoke the quality gate (the 'green' definition) ─────────────
if [ "${SMOKE_GATE:-}" = "1" ]; then
  log "smoking quality_gate.sh (G5 lints + G6 pytest) on $NODE — a few minutes"
  if "${SSH[@]}" -n "$NODE" "source ~/.config/spark/env.sh 2>/dev/null; cd ~/$REMOTE_DIR && bash scripts/quality_gate.sh"; then
    log "quality_gate: GREEN"
  else
    log "quality_gate: RED (baseline — record it; the collapse must end GREEN)"
  fi
fi

cat <<NEXT

[setup-cc $NODE] DONE — workspace at ~/$REMOTE_DIR is synced, overlaid, bootstrapped.

ONE remaining one-time step (subscription auth, your action):
  ssh -t $NODE          # then, inside the claude TUI:
  claude                # run '/login' and approve in the browser; '/status'
                        # must show the Max subscription (NOT an API-key path).
  # Or headless:  ssh -t $NODE 'source ~/.config/spark/env.sh; claude setup-token'
Verify afterwards:  python scripts/spark_cc.py auth --node $NODE
NEXT
