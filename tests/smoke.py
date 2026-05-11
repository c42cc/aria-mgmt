"""Manual smoke tests for UCS components."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_config_loads():
    from src.config import config
    assert config.gemini_model == os.getenv("GEMINI_MODEL", "gemini-3.1-live")
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


def test_audio_resampling():
    import numpy as np
    from src.discord_voice import discord_to_gemini, gemini_to_discord

    # 48kHz stereo → 16kHz mono
    discord_pcm = np.zeros(48000 * 2, dtype=np.int16).tobytes()
    gemini_pcm = discord_to_gemini(discord_pcm)
    assert len(gemini_pcm) == 16000 * 2  # 16000 samples * 2 bytes

    # 16kHz mono → 48kHz stereo
    mono_pcm = np.zeros(16000, dtype=np.int16).tobytes()
    stereo_pcm = gemini_to_discord(mono_pcm)
    assert len(stereo_pcm) == 48000 * 2 * 2  # 48000 samples * 2 channels * 2 bytes

    print("PASS: audio resampling")


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
    test_audio_resampling()
    test_cursor_wrapper_healthcheck()
    print("\nAll smoke tests passed.")
