"""Manual smoke tests for UCS components."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_config_loads():
    from src.config import config
    assert config.gemini_model == os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    assert config.claude_model == os.getenv("CLAUDE_MODEL", "claude-opus-4-6")
    print("PASS: config loads")


def test_db_init():
    from src.db import init_db, get_connection
    init_db()
    with get_connection() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r["name"] for r in tables}
    assert "cursor_sessions" in table_names
    assert "events" in table_names
    assert "planning_history" in table_names
    print("PASS: db initializes")


def test_prompts_load():
    from src.prompts import load_template
    planning = load_template("planning")
    assert "implementation plan" in planning.lower() or "plan" in planning.lower()
    print("PASS: prompts load")


def test_voice_bridge_module():
    """Voice bridge class is importable and exposes the expected public API."""
    from src.discord_voice import voice_bridge, VoiceBridge
    assert isinstance(voice_bridge, VoiceBridge)
    assert voice_bridge.alive is False
    for name in ("start", "join", "leave", "send_audio", "close", "register_audio_callback"):
        assert callable(getattr(voice_bridge, name)), f"missing public method: {name}"
    print("PASS: voice bridge module surface")


def test_voice_bridge_node_syntax():
    """The Node sidecar parses (does not exercise discord.js login)."""
    import subprocess
    bridge_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "discord_voice_bridge")
    index = os.path.join(bridge_dir, "index.js")
    if not os.path.isfile(index):
        print(f"FAIL: {index} missing")
        raise AssertionError(index)
    result = subprocess.run(["node", "--check", index], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    print("PASS: voice bridge index.js parses")


def test_cursor_wrapper_healthcheck():
    import subprocess
    wrapper_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cursor_wrapper")
    if not os.path.exists(os.path.join(wrapper_dir, "node_modules")):
        print("SKIP: cursor_wrapper node_modules not installed")
        return
    result = subprocess.run(
        ["node", "index.js", "--healthcheck"],
        cwd=wrapper_dir,
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert '"ok"' in result.stdout
    print("PASS: cursor wrapper healthcheck")


if __name__ == "__main__":
    test_config_loads()
    test_db_init()
    test_prompts_load()
    test_voice_bridge_module()
    test_voice_bridge_node_syntax()
    test_cursor_wrapper_healthcheck()
    print("\nAll smoke tests passed.")
