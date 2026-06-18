#!/usr/bin/env bash
# bootstrap_cc.sh — rebuild the non-committed local environment for this repo,
# then verify the result instead of assuming it. Idempotent: safe to re-run.
#
# This is the spark overlay copy (ops/spark/cc_workspace/), kept faithful to the
# live_visuals_4_CC version so the spark environment matches the Mac's.
#
# Scope (deliberately narrow — fewest moving parts):
#   - The Claude Code control plane (CLAUDE.md, .claude/, .mcp.json) is OVERLAID
#     onto this checkout by setup_cc_workspace.sh, so this script does NOT
#     scaffold it.
#   - Claude Code itself and its auth (the Max subscription via `claude /login`)
#     are machine prerequisites, verified here, never mutated here.
#   - What this rebuilds is only what `.gitignore` omits: the Python venvs and the
#     Node sidecar `node_modules` trees.
#
# Prereqs (verified below, not installed): claude, node/npm, python3.11 (or uv).
# Usage:  scripts/bootstrap_cc.sh

set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '\033[32mok:\033[0m %s\n' "$*"; }
die()  { printf '\033[31mbootstrap: %s\033[0m\n' "$*" >&2; exit 1; }

# ── Prereqs ──────────────────────────────────────────────────────────────────
step "PREREQS"
command -v node  >/dev/null || die "node not on PATH"
command -v npm   >/dev/null || die "npm not on PATH"
PYBIN=""
if command -v uv >/dev/null; then
  PYBIN="uv"; ok "uv $(uv --version | awk '{print $2}')"
elif command -v python3.11 >/dev/null; then
  PYBIN="python3.11"; ok "python3.11 $(python3.11 --version | awk '{print $2}')"
else
  die "need uv or python3.11 (repo pins Python 3.11 via .python-version)"
fi
command -v claude >/dev/null && ok "claude $(claude --version 2>/dev/null | head -1)" \
  || echo "warn: claude CLI not found — install Claude Code before using this repo"

# ── Python venv (root) ───────────────────────────────────────────────────────
# We never source .env here (keeps ANTHROPIC_API_KEY out of any process that
# could shadow Claude Code's subscription).
make_venv() {  # $1 = venv dir, $2 = requirements file
  local venv="$1" req="$2"
  if [ "$PYBIN" = "uv" ]; then
    [ -d "$venv" ] || uv venv --python 3.11 "$venv"
    VIRTUAL_ENV="$venv" uv pip install --quiet -r "$req"
  else
    [ -d "$venv" ] || python3.11 -m venv "$venv"
    "$venv/bin/pip" install --quiet --upgrade pip
    "$venv/bin/pip" install --quiet -r "$req"
  fi
}

step "PYTHON VENV (root, 3.11)"
make_venv "$ROOT/.venv" "$ROOT/requirements.txt"
ok ".venv ready"

step "PYTHON VENV (hologram_service)"
if [ -f "$ROOT/hologram_service/requirements.txt" ]; then
  make_venv "$ROOT/hologram_service/.venv" "$ROOT/hologram_service/requirements.txt"
  ok "hologram_service/.venv ready"
fi

# ── Node sidecars ────────────────────────────────────────────────────────────
# THE ONE EYE (tools/render_at_pose) is load-bearing: the live SEE stage and the
# depiction tests spawn it; 7 tests fail until it is installed (dev-environment).
node_ci() {  # $1 = dir containing package.json + package-lock.json
  local d="$1"
  [ -f "$d/package-lock.json" ] || die "no lockfile in $d (npm ci needs one)"
  npm ci --prefix "$d" --no-audit --no-fund
}

step "NODE — root (tsc timing lint)"
node_ci "$ROOT" && ok "root node_modules ready"

step "NODE — tools/render_at_pose (THE ONE EYE sidecar)"
if [ -f "$ROOT/tools/render_at_pose/package.json" ]; then
  node_ci "$ROOT/tools/render_at_pose" && ok "render_at_pose node_modules ready"
fi

step "NODE — hologram_service/evaluator"
[ -f "$ROOT/hologram_service/evaluator/package.json" ] \
  && node_ci "$ROOT/hologram_service/evaluator" && ok "evaluator node_modules ready"

# ── Verify (do not assume success) ───────────────────────────────────────────
step "VERIFY"
PY="$ROOT/.venv/bin/python"
"$PY" - <<'PY' || die "python dependency import smoke FAILED"
import fastapi, uvicorn, httpx, websockets, yaml, jsonschema, pytest, PIL, imageio, numpy  # noqa
print("  py deps import ok")
PY
"$PY" -c "from configs.parameters import params; print('  params loader ok: mastertest.port =', int(params('mastertest.port')))" \
  || die "configs.parameters loader FAILED (the app's single config home)"
npm run --silent lint:timing >/dev/null 2>&1 && ok "tsc timing lint passes" \
  || echo "warn: 'npm run lint:timing' nonzero (inspect separately; not a bootstrap blocker)"

step "DONE"
echo "Local environment rebuilt. Subscription check: 'claude /status' must show the Max"
echo "subscription (NOT an API-key path). Never 'source .env' in the shell that runs claude."
