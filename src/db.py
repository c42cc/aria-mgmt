"""SQLite schema and queries for session state and event logging."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .config import config

DB_PATH = os.path.join(config.data_dir, "state.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor_sessions (
    session_id   TEXT PRIMARY KEY,
    project      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running',
    started_at   TEXT NOT NULL,
    last_event_at TEXT,
    last_event_summary TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    params       TEXT,
    result       TEXT,
    duration_ms  INTEGER,
    session_key  TEXT,
    token_cost_usd REAL
);

CREATE TABLE IF NOT EXISTS planning_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key  TEXT NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    timestamp    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discord_threads (
    thread_id    TEXT PRIMARY KEY,
    session_key  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_name   TEXT NOT NULL,
    version       INTEGER NOT NULL,
    content       TEXT NOT NULL,
    metadata_json TEXT,
    origin        TEXT NOT NULL DEFAULT 'user',
    created_at    TEXT NOT NULL,
    UNIQUE(prompt_name, version)
);

CREATE TABLE IF NOT EXISTS eval_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_name     TEXT NOT NULL,
    prompt_version  INTEGER NOT NULL,
    metric          TEXT NOT NULL,
    score           REAL NOT NULL,
    sample_size     INTEGER,
    detail_json     TEXT,
    evaluated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loop_executions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name           TEXT NOT NULL,
    session_key         TEXT,
    prompt_template     TEXT,
    model_id            TEXT NOT NULL,
    routing_path        TEXT NOT NULL DEFAULT 'legacy',
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    cost_usd            REAL,
    latency_ms          INTEGER,
    iterations          INTEGER,
    status              TEXT NOT NULL,
    context_truncated   INTEGER NOT NULL DEFAULT 0,
    turns_dropped       INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT NOT NULL,
    finished_at         TEXT
);

CREATE TABLE IF NOT EXISTS session_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key  TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    product      TEXT NOT NULL,
    inputs_json  TEXT NOT NULL,
    outputs_json TEXT NOT NULL,
    context_json TEXT,
    timestamp    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verdicts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    product              TEXT NOT NULL,
    session_id           TEXT NOT NULL,
    verdict              TEXT NOT NULL,
    score                REAL NOT NULL,
    reasons              TEXT,
    judged_at            TEXT NOT NULL,
    anchor_floor         TEXT,
    anchor_reports_json  TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _migrate_loop_executions(conn)
        _migrate_verdicts_anchors(conn)


def _migrate_loop_executions(conn: sqlite3.Connection) -> None:
    """Add columns introduced by the UCS audit (routing_path, context_truncated, turns_dropped)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(loop_executions)").fetchall()}
    if "routing_path" not in cols:
        conn.execute("ALTER TABLE loop_executions ADD COLUMN routing_path TEXT NOT NULL DEFAULT 'legacy'")
    if "context_truncated" not in cols:
        conn.execute("ALTER TABLE loop_executions ADD COLUMN context_truncated INTEGER NOT NULL DEFAULT 0")
    if "turns_dropped" not in cols:
        conn.execute("ALTER TABLE loop_executions ADD COLUMN turns_dropped INTEGER NOT NULL DEFAULT 0")


def _migrate_verdicts_anchors(conn: sqlite3.Connection) -> None:
    """Add anchor_floor and anchor_reports_json columns to verdicts table."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(verdicts)").fetchall()}
    if "anchor_floor" not in cols:
        conn.execute("ALTER TABLE verdicts ADD COLUMN anchor_floor TEXT")
    if "anchor_reports_json" not in cols:
        conn.execute("ALTER TABLE verdicts ADD COLUMN anchor_reports_json TEXT")


def log_event(
    tool_name: str,
    params: dict[str, Any] | None = None,
    result: str | None = None,
    duration_ms: int | None = None,
    session_key: str | None = None,
    token_cost_usd: float | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO events (timestamp, tool_name, params, result, duration_ms, session_key, token_cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), tool_name, json.dumps(params), result, duration_ms, session_key, token_cost_usd),
        )


def get_daily_spend() -> float:
    today = datetime.now(timezone.utc).date().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(token_cost_usd), 0) as total FROM events WHERE timestamp >= ?",
            (today,),
        ).fetchone()
    return float(row["total"]) if row else 0.0


def get_planning_history(session_key: str) -> list[dict[str, str]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT role, content FROM planning_history WHERE session_key = ? ORDER BY id",
            (session_key,),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def append_planning_history(session_key: str, role: str, content: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO planning_history (session_key, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (session_key, role, content, _now()),
        )


def upsert_cursor_session(
    session_id: str, project: str, status: str = "running"
) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO cursor_sessions (session_id, project, status, started_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET status=excluded.status",
            (session_id, project, status, _now()),
        )


def update_cursor_session_event(session_id: str, summary: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE cursor_sessions SET last_event_at = ?, last_event_summary = ? WHERE session_id = ?",
            (_now(), summary, session_id),
        )


def get_active_cursor_sessions() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM cursor_sessions WHERE status IN ('running', 'waiting')"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Prompt version control
# ---------------------------------------------------------------------------

def get_next_prompt_version(prompt_name: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) as v FROM prompt_versions WHERE prompt_name = ?",
            (prompt_name,),
        ).fetchone()
    return int(row["v"]) + 1


def insert_prompt_version(
    prompt_name: str, version: int, content: str,
    origin: str = "user", metadata_json: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO prompt_versions "
            "(prompt_name, version, content, metadata_json, origin, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (prompt_name, version, content, metadata_json, origin, _now()),
        )


def get_prompt_versions(prompt_name: str) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, prompt_name, version, origin, created_at, "
            "LENGTH(content) as content_length "
            "FROM prompt_versions WHERE prompt_name = ? ORDER BY version",
            (prompt_name,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_prompt_version_content(prompt_name: str, version: int) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT content FROM prompt_versions WHERE prompt_name = ? AND version = ?",
            (prompt_name, version),
        ).fetchone()
    return row["content"] if row else None


# ---------------------------------------------------------------------------
# Session records (EMIT layer) and verdicts (SURFACE layer)
# ---------------------------------------------------------------------------

TOOL_PRODUCT_MAP: dict[str, str] = {
    "plan_with_claude": "planning",
    "build_with_cursor": "build",
    "query_cursor": "build",
    "do_with_claude": "agent",
    "quick_email_check": "quick_read",
    "quick_calendar": "quick_read",
    "remember": "memory",
    "recall": "memory",
    "edit_prompt": "prompt_mgmt",
    "rollback_prompt": "prompt_mgmt",
    "show_prompt": "prompt_mgmt",
    "list_prompts": "prompt_mgmt",
    "prompt_versions": "prompt_mgmt",
    "reload_prompts": "prompt_mgmt",
    "get_focused_app": "system",
    "focus_app": "system",
    "dictate_into_focused_app": "system",
    "cursor_status": "system",
    "cancel_current_task": "system",
    "confirm_action": "system",
    "spicylit_generate_outline": "spicylit",
    "spicylit_joi_session": "spicylit",
}

JUDGE_WORTHY_PRODUCTS = frozenset({
    "planning", "build", "agent", "quick_read", "memory", "prompt_mgmt", "spicylit",
})


def tool_to_product(tool_name: str) -> str:
    return TOOL_PRODUCT_MAP.get(tool_name, "unknown")


def record_session(
    session_key: str,
    tool_name: str,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> int | None:
    """Write one session record. Returns the row id, or None on failure."""
    product = tool_to_product(tool_name)
    try:
        with get_connection() as conn:
            cur = conn.execute(
                "INSERT INTO session_records "
                "(session_key, tool_name, product, inputs_json, outputs_json, context_json, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_key,
                    tool_name,
                    product,
                    json.dumps(inputs, default=str),
                    json.dumps(outputs, default=str),
                    json.dumps(context, default=str) if context else None,
                    _now(),
                ),
            )
            return cur.lastrowid
    except Exception:
        log.warning("Failed to write session_record", exc_info=True)
        return None


def get_session_record(record_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM session_records WHERE id = ?", (record_id,)
        ).fetchone()
    return dict(row) if row else None


def write_verdict(
    product: str,
    session_id: str,
    verdict: str,
    score: float,
    reasons: list[str],
    anchor_floor: str | None = None,
    anchor_reports_json: str | None = None,
) -> None:
    """Write one verdict row. Never propagates exceptions."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO verdicts "
                "(product, session_id, verdict, score, reasons, judged_at, anchor_floor, anchor_reports_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (product, session_id, verdict, score, json.dumps(reasons), _now(),
                 anchor_floor, anchor_reports_json),
            )
    except Exception:
        log.warning("Failed to write verdict", exc_info=True)


def get_recent_verdicts(hours: int = 24) -> list[dict[str, Any]]:
    """Get verdicts from the last N hours."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT product, verdict, score, reasons, judged_at "
            "FROM verdicts WHERE judged_at >= ? ORDER BY judged_at DESC",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_correctness_summary(hours: int = 24) -> dict[str, dict[str, Any]]:
    """Correctness rates by product over the last N hours."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT product, verdict, COUNT(*) as cnt "
            "FROM verdicts WHERE judged_at >= ? GROUP BY product, verdict",
            (cutoff,),
        ).fetchall()
    summary: dict[str, dict[str, Any]] = {}
    for r in rows:
        p = r["product"]
        if p not in summary:
            summary[p] = {"total": 0, "correct": 0, "degraded": 0, "failed": 0}
        summary[p][r["verdict"]] = r["cnt"]
        summary[p]["total"] += r["cnt"]
    for p in summary:
        total = summary[p]["total"]
        summary[p]["correctness_rate"] = summary[p]["correct"] / total if total else 0.0
    return summary


# ---------------------------------------------------------------------------
# Loop execution logging (observability — must never break the hot path)
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


def log_loop_execution(
    tool_name: str,
    model_id: str,
    status: str,
    started_at: str,
    session_key: str | None = None,
    prompt_template: str | None = None,
    routing_path: str = "legacy",
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
    iterations: int | None = None,
    context_truncated: bool = False,
    turns_dropped: int = 0,
) -> None:
    """Write one row to loop_executions. Never propagates exceptions."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO loop_executions "
                "(tool_name, session_key, prompt_template, model_id, routing_path, "
                "tokens_in, tokens_out, cost_usd, latency_ms, iterations, "
                "status, context_truncated, turns_dropped, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tool_name, session_key, prompt_template, model_id, routing_path,
                 tokens_in, tokens_out, cost_usd, latency_ms, iterations,
                 status, int(context_truncated), turns_dropped, started_at, _now()),
            )
    except Exception:
        log.warning("Failed to write loop_execution row", exc_info=True)
