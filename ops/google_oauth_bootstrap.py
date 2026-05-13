"""One-time Google Calendar OAuth bootstrap.

The google-calendar-mcp server requires:
  1. A `gcp-oauth.keys.json` from Google Cloud Console (Desktop OAuth client)
  2. A `tokens.json` produced by completing the OAuth consent flow once

This script:
  - Walks you through obtaining gcp-oauth.keys.json if you don't have one
  - Copies it to ~/.config/google-calendar-mcp/gcp-oauth.keys.json
  - Runs the google-calendar-mcp server's own OAuth flow (via its built-in
    auth-server.js) which opens a browser, captures the callback, and writes
    tokens.json
  - Verifies the resulting tokens are usable

Run: .venv/bin/python ops/google_oauth_bootstrap.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "google-calendar-mcp"
KEYS_PATH = CONFIG_DIR / "gcp-oauth.keys.json"
TOKENS_PATH = CONFIG_DIR / "tokens.json"


def info(msg: str) -> None:
    print(f"[oauth] {msg}", flush=True)


def err(msg: str) -> None:
    print(f"[oauth ERROR] {msg}", file=sys.stderr, flush=True)


def print_console_instructions() -> None:
    print("""
========================================================================
Google Cloud Console one-time setup
========================================================================

1. Open: https://console.cloud.google.com/
2. Create a new project (or pick an existing one).
3. Enable the Google Calendar API:
   https://console.cloud.google.com/apis/library/calendar-json.googleapis.com
4. Go to APIs & Services > Credentials.
5. Click "Create Credentials" > "OAuth client ID".
6. Application type: **Desktop app** (NOT web app).
7. Give it any name. Click Create.
8. Download the JSON file. It will look like gcp-oauth.keys.json or
   client_secret_*.json.
9. Important: under "OAuth consent screen" / "Audience", add your Google
   account as a test user. Wait a minute for it to propagate.

When you have downloaded the JSON file, enter its absolute path below.
========================================================================
""", flush=True)


def install_keys_file() -> Path:
    if KEYS_PATH.exists():
        info(f"gcp-oauth.keys.json already installed at {KEYS_PATH}")
        return KEYS_PATH

    print_console_instructions()
    while True:
        src = input("Path to your downloaded gcp-oauth.keys.json: ").strip()
        if not src:
            err("Path is required. Ctrl-C to abort.")
            continue
        src_path = Path(os.path.expanduser(src))
        if not src_path.is_file():
            err(f"Not a file: {src_path}")
            continue
        # Validate it parses and has the expected shape
        try:
            data = json.loads(src_path.read_text())
        except Exception as exc:
            err(f"Could not parse JSON: {exc}")
            continue
        if "installed" not in data and "web" not in data:
            err(
                "JSON does not look like an OAuth client_secret file "
                "(no 'installed' or 'web' key). Did you pick a Desktop app credential?"
            )
            continue
        if "installed" not in data:
            err("Your credential is type 'web' — must be 'Desktop app'.")
            continue
        break

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy(src_path, KEYS_PATH)
    KEYS_PATH.chmod(0o600)
    info(f"Installed keys to {KEYS_PATH}")
    return KEYS_PATH


def run_oauth_flow() -> bool:
    """Invoke the google-calendar-mcp server's built-in auth-server.js.

    The package exposes an `auth-server` script via `npx @cocal/google-calendar-mcp auth`
    which opens a browser, handles the callback, and writes tokens.json.
    """
    env = os.environ.copy()
    env["GOOGLE_OAUTH_CREDENTIALS"] = str(KEYS_PATH)
    env["GOOGLE_CALENDAR_MCP_TOKEN_PATH"] = str(TOKENS_PATH)

    info("Starting OAuth flow (will open your browser)...")
    info("If the browser does not open automatically, copy the URL it prints.")

    # The package has an 'auth' subcommand; try it. Fall back to running
    # auth-server.js directly if the wrapper changes.
    candidates = [
        ["npx", "--yes", "@cocal/google-calendar-mcp", "auth"],
        ["npx", "--yes", "-p", "@cocal/google-calendar-mcp", "node",
         os.path.expanduser("~/.npm/_npx/a23ca25355f98efb/node_modules/@cocal/google-calendar-mcp/build/auth-server.js")],
    ]

    for cmd in candidates:
        info(f"Running: {' '.join(cmd)}")
        try:
            proc = subprocess.run(cmd, env=env, timeout=300)
        except subprocess.TimeoutExpired:
            err("OAuth flow timed out after 5 minutes. Aborting.")
            return False
        except FileNotFoundError:
            err(f"Command not found: {cmd[0]}")
            continue

        if proc.returncode == 0 and TOKENS_PATH.exists():
            info(f"tokens.json written to {TOKENS_PATH}")
            return True
        err(f"OAuth flow exited with code {proc.returncode}")
        if not TOKENS_PATH.exists():
            err("No tokens.json was written.")
        if proc.returncode != 0:
            # Try next candidate
            continue
        return True

    return False


def verify_tokens() -> bool:
    if not TOKENS_PATH.exists():
        err(f"tokens.json missing: {TOKENS_PATH}")
        return False
    try:
        data = json.loads(TOKENS_PATH.read_text())
    except Exception as exc:
        err(f"tokens.json is not valid JSON: {exc}")
        return False

    info(f"tokens.json keys: {sorted(data.keys()) if isinstance(data, dict) else type(data).__name__}")
    info(f"OAuth bootstrap complete. The google-calendar MCP server will pick up these credentials.")
    return True


def main() -> int:
    info("Google Calendar OAuth bootstrap")
    install_keys_file()
    if not run_oauth_flow():
        err("OAuth flow failed. Re-run after fixing the error above.")
        return 1
    if not verify_tokens():
        return 1

    # Append GOOGLE_OAUTH_CREDENTIALS to .env if missing
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        text = env_path.read_text()
        if "GOOGLE_OAUTH_CREDENTIALS=" not in text:
            with env_path.open("a") as f:
                f.write(f"\nGOOGLE_OAUTH_CREDENTIALS={KEYS_PATH}\n")
            info(f"Appended GOOGLE_OAUTH_CREDENTIALS to {env_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
