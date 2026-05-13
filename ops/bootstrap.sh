#!/usr/bin/env bash
# One-command fresh-machine setup for UCS.
#
# Steps (each is idempotent; rerunning is safe):
#   1. Verify Python 3.12 is installed
#   2. Create/update virtualenv and install the package in editable mode
#   3. Install Node deps in cursor_wrapper/
#   4. Install required global npm MCP servers
#   5. Build the EventKitCLI Swift binary for mcp-macos
#   6. (Interactive) Run Google OAuth bootstrap if creds missing
#   7. (Interactive) Run permissions helper if any TCC prompt is needed
#   8. Run preflight and report
#
# Re-run safely after macOS updates, npm package updates, or Python upgrades.

set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"

banner() {
    echo ""
    echo "========================================================================"
    echo "  $1"
    echo "========================================================================"
}

# -----------------------------------------------------------------------
# 1. Python 3.12 venv
# -----------------------------------------------------------------------
banner "1/8 Python virtualenv"

PYTHON_CMD=""
for cand in python3.12 python3.13; do
    if command -v "$cand" >/dev/null 2>&1; then
        PYTHON_CMD="$cand"
        break
    fi
done

if [[ -z "$PYTHON_CMD" ]]; then
    echo "ERROR: Python 3.12 or 3.13 not found. Install with:" >&2
    echo "  brew install python@3.12" >&2
    exit 1
fi
echo "Using $PYTHON_CMD ($("$PYTHON_CMD" --version))"

if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating venv at $VENV_DIR..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

# -----------------------------------------------------------------------
# 2. Python deps (editable install)
# -----------------------------------------------------------------------
banner "2/8 Python dependencies"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -e . --quiet
echo "OK"

# -----------------------------------------------------------------------
# 3. Node deps (cursor_wrapper)
# -----------------------------------------------------------------------
banner "3/8 Cursor wrapper Node deps"
if ! command -v npm >/dev/null 2>&1; then
    echo "ERROR: npm not found. Install Node.js first." >&2
    exit 1
fi

if [[ ! -d "$PROJECT_ROOT/cursor_wrapper/node_modules" ]]; then
    (cd "$PROJECT_ROOT/cursor_wrapper" && npm install)
else
    echo "node_modules present (skipping install; run 'cd cursor_wrapper && npm install' to update)"
fi

# -----------------------------------------------------------------------
# 4. Global MCP server packages
# -----------------------------------------------------------------------
banner "4/8 MCP server packages (global npm)"

MCP_PKGS=(
    "mcp-macos"
    "@cocal/google-calendar-mcp"
    "@modelcontextprotocol/server-filesystem"
    "mcp-shell-execute"
    "@modelcontextprotocol/server-github"
)

for pkg in "${MCP_PKGS[@]}"; do
    if npm list -g --depth=0 "$pkg" >/dev/null 2>&1; then
        echo "  [OK]   $pkg"
    else
        echo "  [INST] $pkg"
        npm install -g "$pkg" || echo "  WARN: $pkg install failed (npx fallback will still work)"
    fi
done

# -----------------------------------------------------------------------
# 5. Swift binary for mcp-macos
# -----------------------------------------------------------------------
banner "5/8 EventKitCLI Swift binary"
if bash "$PROJECT_ROOT/ops/build_macos_swift.sh" 2>&1 | tail -5; then
    echo "OK"
else
    echo "WARN: Swift build failed. Calendar/Reminders tools will be unavailable until fixed."
    echo "      See above for the specific compiler/SDK error."
fi

# -----------------------------------------------------------------------
# 6. Google OAuth (interactive, optional)
# -----------------------------------------------------------------------
banner "6/8 Google Calendar OAuth"
if [[ -f "$HOME/.config/google-calendar-mcp/gcp-oauth.keys.json" ]] \
   && [[ -f "$HOME/.config/google-calendar-mcp/tokens.json" ]]; then
    echo "  OAuth credentials and tokens already present. Skipping."
else
    echo "  No OAuth credentials found."
    read -rp "Run interactive Google OAuth bootstrap now? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        "$VENV_DIR/bin/python" "$PROJECT_ROOT/ops/google_oauth_bootstrap.py"
    else
        echo "  Skipped. Run manually later: .venv/bin/python ops/google_oauth_bootstrap.py"
    fi
fi

# -----------------------------------------------------------------------
# 7. macOS permissions (interactive, optional)
# -----------------------------------------------------------------------
banner "7/8 macOS permissions"
read -rp "Trigger macOS permission dialogs now? [y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
    bash "$PROJECT_ROOT/ops/grant_permissions.sh"
else
    echo "  Skipped. Run manually later: bash ops/grant_permissions.sh"
    echo "  (Or preflight will tell you exactly which permission is missing.)"
fi

# -----------------------------------------------------------------------
# 8. Preflight verification
# -----------------------------------------------------------------------
banner "8/8 Preflight"
"$VENV_DIR/bin/python" -m src.preflight --no-cursor || true

echo ""
echo "========================================================================"
echo "  Bootstrap complete."
echo "  Next: make run"
echo "========================================================================"
