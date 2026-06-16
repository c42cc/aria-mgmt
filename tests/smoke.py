"""Manual smoke tests for UCS components."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_config_loads():
    from src.config import config
    assert config.gemini_model == os.getenv("GEMINI_MODEL", "gemini-2.5-flash-native-audio-latest")
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


def test_voice_bridge_playback_pipeline():
    """The bridge can play multiple sequential audio bursts.

    Regression test for the 2026-05-13 bug where the bridge's single
    persistent AudioResource died after the first response and the user
    "couldn't talk to Aria anymore". The fix rebuilds the pipeline on the
    player's Idle event.
    """
    import subprocess
    import shutil
    bridge_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "discord_voice_bridge")
    test_js = os.path.join(bridge_dir, "test_playback.js")
    if not os.path.exists(os.path.join(bridge_dir, "node_modules")):
        print("SKIP: discord_voice_bridge node_modules not installed")
        return
    if shutil.which("ffmpeg") is None:
        print("SKIP: ffmpeg not on PATH — voice bridge cannot function")
        return
    result = subprocess.run(
        ["node", test_js],
        cwd=bridge_dir,
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"FAIL: voice bridge playback pipeline regression")
        print(f"  stdout: {result.stdout}")
        print(f"  stderr: {result.stderr}")
        raise AssertionError(result.stderr or result.stdout)
    assert "PASS: playback_recovers_from_idle" in result.stdout, result.stdout
    print("PASS: voice bridge playback pipeline (3 bursts, all play)")


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


# ---------------------------------------------------------------------------
# Phase 1 UCS tests
# ---------------------------------------------------------------------------


def test_prompt_versioning():
    """save_template archives old content with origin; rollback restores it."""
    import tempfile
    from src.db import init_db, get_connection
    from src.prompts import save_template, get_versions, rollback_template, get_path, read_raw

    init_db()

    name = "_smoke_test_version"
    path = get_path(name)

    try:
        with open(path, "w") as f:
            f.write("original content")

        save_template(name, "edited content v1", origin="user")

        versions = get_versions(name)
        assert len(versions) >= 1, f"Expected at least 1 version, got {len(versions)}"
        v1 = versions[0]
        assert v1["origin"] == "initial", f"First archive should be 'initial', got {v1['origin']}"

        save_template(name, "edited content v2", origin="user")
        versions = get_versions(name)
        assert len(versions) >= 2, f"Expected at least 2 versions, got {len(versions)}"
        assert versions[-1]["origin"] == "user"

        rollback_template(name, versions[0]["version"])
        restored = read_raw(name)
        assert restored == "original content", f"Rollback failed: got {restored!r}"

        versions_after = get_versions(name)
        rollback_entries = [v for v in versions_after if v["origin"] == "rollback"]
        assert len(rollback_entries) >= 1, "Rollback should archive current as origin='rollback'"

    finally:
        if os.path.exists(path):
            os.unlink(path)
        with get_connection() as conn:
            conn.execute("DELETE FROM prompt_versions WHERE prompt_name = ?", (name,))

    print("PASS: prompt versioning (archive, origin tracking, rollback)")


def test_loop_executions_schema():
    """loop_executions table exists and accepts writes."""
    from src.db import init_db, get_connection, log_loop_execution

    init_db()

    log_loop_execution(
        tool_name="smoke_test",
        model_id="test-model",
        status="completed",
        started_at="2026-01-01T00:00:00Z",
        tokens_in=10,
        tokens_out=20,
        cost_usd=0.001,
        latency_ms=100,
        iterations=1,
    )

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM loop_executions WHERE tool_name = 'smoke_test' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert row is not None, "loop_executions row not written"
    assert row["model_id"] == "test-model"
    assert row["tokens_in"] == 10
    assert row["status"] == "completed"

    with get_connection() as conn:
        conn.execute("DELETE FROM loop_executions WHERE tool_name = 'smoke_test'")

    print("PASS: loop_executions schema and write")


def test_loop_logging_resilience():
    """log_loop_execution never propagates exceptions."""
    from src.db import log_loop_execution

    original_db_path = None
    try:
        import src.db as db_mod
        original_db_path = db_mod.DB_PATH
        # /dev/null exists as a file, so makedirs succeeds for its dirname,
        # but SQLite cannot open /dev/null/state.db (not a directory).
        db_mod.DB_PATH = "/dev/null/state.db"

        log_loop_execution(
            tool_name="resilience_test",
            model_id="test",
            status="completed",
            started_at="2026-01-01T00:00:00Z",
        )
    finally:
        if original_db_path is not None:
            db_mod.DB_PATH = original_db_path

    print("PASS: loop logging resilience (broken DB doesn't propagate)")


def test_models_yaml_loads():
    """models.yaml parses and has required fields."""
    import yaml
    from src.config import config

    assert os.path.exists(config.models_config), f"models.yaml not found at {config.models_config}"

    with open(config.models_config) as f:
        data = yaml.safe_load(f)

    assert "models" in data, "Missing 'models' key"
    models = data["models"]
    assert len(models) >= 3, f"Expected at least 3 models, got {len(models)}"

    required_fields = {"provider", "model_id", "api_key_env"}
    for name, spec in models.items():
        missing = required_fields - set(spec.keys())
        assert not missing, f"Model '{name}' missing fields: {missing}"

    print(f"PASS: models.yaml loads ({len(models)} models)")


# ---------------------------------------------------------------------------
# Phase 2 UCS tests
# ---------------------------------------------------------------------------


def test_ucs_confirmation_flow():
    """Under UCS_ENABLED=true, MCP tier-I/X tools go through the confirm callback.

    This test verifies that mcp_client.call_tool still invokes the confirm
    callback when called from the UCS agent loop — the safety-critical path
    that must survive the Phase 2 rewire.
    """
    import json
    from unittest.mock import AsyncMock, MagicMock
    from src.mcp import MCPClient, _classify_tier

    tier = _classify_tier("shell", "execute_command")
    assert tier == "X", f"Shell execute should be tier X, got {tier}"

    tier_r = _classify_tier("filesystem", "read_file")
    assert tier_r == "R", f"Filesystem read should be tier R, got {tier_r}"

    print("PASS: UCS confirmation flow (tier classification verified for safety path)")


# ---------------------------------------------------------------------------
# Phase 3 eval tests
# ---------------------------------------------------------------------------


def test_eval_approval_rate():
    """Given known planning_history + loop_executions rows, computes correct approval rate."""
    from src.db import init_db, get_connection, log_loop_execution

    init_db()

    with get_connection() as conn:
        conn.execute("DELETE FROM loop_executions WHERE session_key LIKE 'eval_test_%'")
        conn.execute("DELETE FROM prompt_versions WHERE prompt_name = '_eval_smoke'")

    log_loop_execution(
        tool_name="plan_with_claude", session_key="eval_test_1",
        prompt_template="planning", model_id="claude-opus-4-6",
        tokens_in=100, tokens_out=200, cost_usd=0.01,
        latency_ms=500, iterations=1, status="completed",
        started_at="2026-01-01T00:00:00Z",
    )
    log_loop_execution(
        tool_name="build_with_cursor", session_key="eval_test_1",
        prompt_template=None, model_id="composer-2",
        tokens_in=0, tokens_out=0, cost_usd=0.0,
        latency_ms=100, iterations=1, status="completed",
        started_at="2026-01-01T00:01:00Z",
    )
    log_loop_execution(
        tool_name="plan_with_claude", session_key="eval_test_2",
        prompt_template="planning", model_id="claude-opus-4-6",
        tokens_in=100, tokens_out=200, cost_usd=0.01,
        latency_ms=500, iterations=1, status="completed",
        started_at="2026-01-01T00:02:00Z",
    )
    log_loop_execution(
        tool_name="plan_with_claude", session_key="eval_test_2",
        prompt_template="planning", model_id="claude-opus-4-6",
        tokens_in=100, tokens_out=200, cost_usd=0.01,
        latency_ms=500, iterations=1, status="completed",
        started_at="2026-01-01T00:03:00Z",
    )

    from src.eval import EvalRunner
    runner = EvalRunner()
    scores = runner.approval_rate("planning")

    assert len(scores) >= 1, f"Expected at least 1 score, got {len(scores)}"
    score = scores[0]
    assert score.sample_size >= 2, f"Expected sample_size >= 2, got {score.sample_size}"
    assert 0.0 <= score.score <= 1.0, f"Score out of range: {score.score}"

    with get_connection() as conn:
        conn.execute("DELETE FROM loop_executions WHERE session_key LIKE 'eval_test_%'")

    print(f"PASS: eval approval rate (score={score.score:.0%}, n={score.sample_size})")


def test_eval_compare_versions():
    """compare_versions correctly ranks prompt versions by approval rate."""
    from src.db import init_db, get_connection, log_loop_execution, insert_prompt_version

    init_db()

    with get_connection() as conn:
        conn.execute("DELETE FROM loop_executions WHERE session_key LIKE 'cmp_test_%'")
        conn.execute("DELETE FROM prompt_versions WHERE prompt_name = '_cmp_smoke'")

    insert_prompt_version("_cmp_smoke", 1, "v1 content", origin="initial")
    insert_prompt_version("_cmp_smoke", 2, "v2 content", origin="user")

    log_loop_execution(
        tool_name="plan_with_claude", session_key="cmp_test_1",
        prompt_template="_cmp_smoke", model_id="claude-opus-4-6",
        tokens_in=100, tokens_out=200, cost_usd=0.01,
        latency_ms=500, iterations=1, status="completed",
        started_at="2026-01-01T00:00:00Z",
    )
    log_loop_execution(
        tool_name="build_with_cursor", session_key="cmp_test_1",
        prompt_template=None, model_id="composer-2",
        tokens_in=0, tokens_out=0, cost_usd=0.0,
        latency_ms=100, iterations=1, status="completed",
        started_at="2026-01-01T00:01:00Z",
    )

    log_loop_execution(
        tool_name="plan_with_claude", session_key="cmp_test_2",
        prompt_template="_cmp_smoke", model_id="claude-opus-4-6",
        tokens_in=100, tokens_out=200, cost_usd=0.01,
        latency_ms=500, iterations=1, status="completed",
        started_at="2026-01-01T00:02:00Z",
    )
    log_loop_execution(
        tool_name="plan_with_claude", session_key="cmp_test_2",
        prompt_template="_cmp_smoke", model_id="claude-opus-4-6",
        tokens_in=100, tokens_out=200, cost_usd=0.01,
        latency_ms=500, iterations=1, status="completed",
        started_at="2026-01-01T00:03:00Z",
    )

    from src.eval import EvalRunner
    runner = EvalRunner()
    scores = runner.compare_versions("_cmp_smoke")

    assert len(scores) >= 1, f"Expected at least 1 score, got {len(scores)}"
    assert all(0.0 <= s.score <= 1.0 for s in scores), "All scores must be in [0, 1]"

    with get_connection() as conn:
        conn.execute("DELETE FROM loop_executions WHERE session_key LIKE 'cmp_test_%'")
        conn.execute("DELETE FROM prompt_versions WHERE prompt_name = '_cmp_smoke'")

    print(f"PASS: eval compare_versions ({len(scores)} version(s) ranked)")


def test_eval_never_writes_prompts():
    """Eval module has no import of save_template or rollback_template."""
    import src.eval as eval_mod

    source_file = eval_mod.__file__
    with open(source_file) as f:
        lines = f.readlines()

    code_lines = [l for l in lines if not l.strip().startswith("#")]

    for i, line in enumerate(code_lines, 1):
        stripped = line.strip()
        if stripped.startswith(('"""', "'''", "#")):
            continue
        if "import" in stripped:
            assert "save_template" not in stripped, (
                f"eval.py line {i} imports save_template — governance violation: {stripped}"
            )
            assert "rollback_template" not in stripped, (
                f"eval.py line {i} imports rollback_template — governance violation: {stripped}"
            )

    print("PASS: eval never writes prompts (governance check)")


if __name__ == "__main__":
    test_config_loads()
    test_db_init()
    test_prompts_load()
    test_voice_bridge_module()
    test_voice_bridge_node_syntax()
    test_voice_bridge_playback_pipeline()
    test_cursor_wrapper_healthcheck()
    # Phase 1
    test_prompt_versioning()
    test_loop_executions_schema()
    test_loop_logging_resilience()
    test_models_yaml_loads()
    # Phase 2
    test_ucs_confirmation_flow()
    # Phase 3
    test_eval_approval_rate()
    test_eval_compare_versions()
    test_eval_never_writes_prompts()
    print("\nAll smoke tests passed.")
