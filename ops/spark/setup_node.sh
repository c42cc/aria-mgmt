#!/usr/bin/env bash
#
# DGX Spark — Section A node setup (Claude Code + base operational layer).
#
# Runs ON a spark node, as the node's normal user, entirely at user level
# (no sudo / no apt). Everything lands in ~/.local/bin so a single PATH entry
# makes the toolchain visible to non-interactive SSH probes. This is the
# uniform path that works on every node regardless of whether sudo/root is
# available — see ops/spark/NODES.md for why we do not use the runbook's
# apt/nodesource path.
#
# Invoke from the Mac:
#   ssh sparkN "ANTHROPIC_API_KEY='\$KEY' bash -s -- <ROLE> <NODE>" < ops/spark/setup_node.sh
# where ROLE is A or B and NODE is spark1 or spark2.
#
# Idempotent: every step checks-then-acts, so re-running is safe and cheap.
# Loud: set -euo pipefail; any failure aborts with a non-zero exit. No silent
# fallbacks. The ANTHROPIC_API_KEY is written to a 0600 file and is never
# echoed to stdout.

set -euo pipefail

ROLE="${1:?usage: setup_node.sh <ROLE=A|B> <NODE=spark1|spark2>}"
NODE="${2:?usage: setup_node.sh <ROLE=A|B> <NODE=spark1|spark2>}"

NODE_MAJOR="22"
MIN_CLAUDE_VERSION="2.1.100"
LOCAL_BIN="$HOME/.local/bin"
SPARK_CFG="$HOME/.config/spark"
CLAUDE_DIR="$HOME/.claude"

log() { printf '[setup %s] %s\n' "$NODE" "$*"; }
die() { printf '[setup %s] FATAL: %s\n' "$NODE" "$*" >&2; exit 1; }

mkdir -p "$LOCAL_BIN" "$SPARK_CFG" "$CLAUDE_DIR"

# --- 0. PATH env file (safe to display; contains no secrets) ----------------
cat > "$SPARK_CFG/env.sh" <<'EOF'
# Sourced by spark acceptance probes and ~/.bashrc. PATH only — no secrets.
export PATH="$HOME/.local/bin:$PATH"
EOF
if ! grep -q 'config/spark/env.sh' "$HOME/.bashrc" 2>/dev/null; then
  printf '\n[ -f ~/.config/spark/env.sh ] && . ~/.config/spark/env.sh\n' >> "$HOME/.bashrc"
fi
export PATH="$LOCAL_BIN:$PATH"

command -v curl >/dev/null 2>&1 || die "curl missing (need HTTPS egress to install)"
command -v jq   >/dev/null 2>&1 || die "jq missing (need it to resolve the Node version)"

# --- 1. Node.js (user-level official tarball, no nvm/apt) -------------------
if "$LOCAL_BIN/node" --version >/dev/null 2>&1; then
  log "node present: $("$LOCAL_BIN/node" --version)"
else
  case "$(uname -m)" in
    aarch64|arm64) NODE_ARCH="arm64" ;;
    x86_64)        NODE_ARCH="x64" ;;
    *) die "unsupported arch $(uname -m) for Node tarball" ;;
  esac
  NODE_VER="$(curl -fsSL https://nodejs.org/dist/index.json \
    | jq -r --arg maj "v$NODE_MAJOR" \
      'map(select(.lts != false)) | map(select(.version | startswith($maj))) | .[0].version')"
  [ -n "$NODE_VER" ] && [ "$NODE_VER" != "null" ] || die "could not resolve latest v$NODE_MAJOR LTS Node"
  log "installing Node $NODE_VER ($NODE_ARCH) to ~/.local/node"
  curl -fsSL "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-$NODE_ARCH.tar.xz" -o /tmp/spark-node.tar.xz \
    || die "Node download failed"
  rm -rf "$HOME/.local/node" && mkdir -p "$HOME/.local/node"
  tar -xJf /tmp/spark-node.tar.xz -C "$HOME/.local/node" --strip-components=1 || die "Node extract failed"
  rm -f /tmp/spark-node.tar.xz
  for b in node npm npx; do ln -sf "$HOME/.local/node/bin/$b" "$LOCAL_BIN/$b"; done
  "$LOCAL_BIN/node" --version >/dev/null 2>&1 || die "Node still not runnable after install"
  log "node installed: $("$LOCAL_BIN/node" --version)"
fi

# --- 2. uv (user-level) -----------------------------------------------------
if "$LOCAL_BIN/uv" --version >/dev/null 2>&1 || command -v uv >/dev/null 2>&1; then
  log "uv present: $(uv --version 2>/dev/null || "$LOCAL_BIN/uv" --version)"
else
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv install failed"
fi

# --- 3. Claude Code (native installer, stable channel) ---------------------
if "$LOCAL_BIN/claude" --version >/dev/null 2>&1; then
  log "claude present: $("$LOCAL_BIN/claude" --version)"
else
  log "installing Claude Code (stable channel)"
  curl -fsSL https://claude.ai/install.sh | bash -s stable || die "Claude Code install failed"
  if [ ! -x "$LOCAL_BIN/claude" ]; then
    found="$(command -v claude 2>/dev/null || find "$HOME/.local" "$CLAUDE_DIR" -maxdepth 4 -type f -name claude 2>/dev/null | head -1)"
    [ -n "$found" ] || die "claude binary not found after install"
    ln -sf "$found" "$LOCAL_BIN/claude"
  fi
  "$LOCAL_BIN/claude" --version >/dev/null 2>&1 || die "claude still not runnable after install"
  log "claude installed: $("$LOCAL_BIN/claude" --version)"
fi

# --- 4. ANTHROPIC_API_KEY (0600 file; headless auth; never echoed) ---------
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  ( umask 077; printf '%s' "$ANTHROPIC_API_KEY" > "$SPARK_CFG/anthropic_key" )
  log "wrote anthropic_key (0600, $(wc -c < "$SPARK_CFG/anthropic_key") bytes)"
elif [ -s "$SPARK_CFG/anthropic_key" ]; then
  log "anthropic_key already present (0600)"
else
  die "ANTHROPIC_API_KEY not provided and no existing key file — headless auth would be blind"
fi

# --- 5. ~/.claude/settings.json --------------------------------------------
# USE_BUILTIN_RIPGREP=1: we do not install a system ripgrep (no apt), so Claude
# must use its bundled binary. minimumVersion floors both nodes to a vetted build.
cat > "$CLAUDE_DIR/settings.json" <<EOF
{
  "autoUpdatesChannel": "stable",
  "minimumVersion": "$MIN_CLAUDE_VERSION",
  "env": {
    "USE_BUILTIN_RIPGREP": "1"
  }
}
EOF
jq -e . "$CLAUDE_DIR/settings.json" >/dev/null 2>&1 || die "settings.json is not valid JSON"
log "wrote ~/.claude/settings.json"

# --- 6. ~/.claude/CLAUDE.md (node identity) --------------------------------
if [ "$ROLE" = "A" ]; then
  ROLE_DESC="worker node A"
  ROLE_EXTRA="In default mode you host one VLM and pull tagging work from the shared queue. Do not start cluster/distributed jobs without explicit instruction."
elif [ "$ROLE" = "B" ]; then
  ROLE_DESC="worker node B"
  ROLE_EXTRA="In default mode you host one VLM and pull tagging work from the shared queue. You may also serve as the rank-0 cluster head when a distributed job is explicitly requested. Do not start cluster/distributed jobs without explicit instruction."
else
  die "ROLE must be A or B (got '$ROLE')"
fi
cat > "$CLAUDE_DIR/CLAUDE.md" <<EOF
You are operating on **$NODE**, $ROLE_DESC of a two-node DGX Spark rig.
128 GB unified memory, one GB10 GPU, ~273 GB/s memory bandwidth.
$ROLE_EXTRA
EOF
log "wrote ~/.claude/CLAUDE.md ($ROLE_DESC)"

# --- 7. filesystem MCP (repo/artifact access) ------------------------------
if "$LOCAL_BIN/claude" mcp list 2>/dev/null | grep -q 'filesystem'; then
  log "filesystem MCP already registered"
else
  log "registering filesystem MCP rooted at \$HOME"
  "$LOCAL_BIN/claude" mcp add filesystem -- npx -y @modelcontextprotocol/server-filesystem "$HOME" \
    || die "claude mcp add filesystem failed"
fi

log "DONE — Section A toolchain in place. Run the acceptance harness from the Mac to verify."
