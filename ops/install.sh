#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== UCS Setup ==="

# Python venv
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$PROJECT_DIR/.venv"
fi

echo "Installing Python dependencies..."
"$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

# Node dependencies
echo "Installing Node.js dependencies..."
cd "$PROJECT_DIR/cursor_wrapper" && npm install

# Data directories
mkdir -p "$PROJECT_DIR/data/mem0"

# Log directory
mkdir -p ~/Library/Logs/voicebot

# .env
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo "Created .env from template — fill in your API keys."
fi

# launchd
echo ""
echo "To install as a launchd service:"
echo "  launchctl bootstrap gui/\$(id -u) $SCRIPT_DIR/com.you.voicebot.plist"
echo ""
echo "To uninstall:"
echo "  launchctl bootout gui/\$(id -u) $SCRIPT_DIR/com.you.voicebot.plist"
echo ""
echo "=== Setup complete ==="
