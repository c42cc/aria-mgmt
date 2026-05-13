"""SQLite schema and queries for session state and event logging."""

from __future__ import annotations

import json
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
