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

-- Claude Code (Agent SDK) sessions Aria drives. Mirrors cursor_sessions, plus
-- `workspace_root` (so a session can be resumed in its own cwd after a restart
-- via ClaudeAgentOptions.resume) and `cost_usd` (Claude Code reports
-- total_cost_usd per turn; this is the durable per-session running total).
CREATE TABLE IF NOT EXISTS claude_sessions (
    session_id     TEXT PRIMARY KEY,
    project        TEXT NOT NULL,
    workspace_root TEXT,
    status         TEXT NOT NULL DEFAULT 'running',
    started_at     TEXT NOT NULL,
    last_event_at  TEXT,
    last_event_summary TEXT,
    cost_usd       REAL NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS thread_summaries (
    sid             TEXT PRIMARY KEY,
    workspace_root  TEXT NOT NULL,
    project_label   TEXT,
    mtime           REAL NOT NULL,
    turns           INTEGER,
    label           TEXT NOT NULL,
    purpose         TEXT,
    did             TEXT,
    status          TEXT,
    open_question   TEXT,
    model_id        TEXT,
    distilled_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ground (
    role        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    path        TEXT,
    detail      TEXT,
    source      TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loop_findings (
    session_key TEXT PRIMARY KEY,
    findings    TEXT NOT NULL,
    status      TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- The Task (Primitive 1): a durable, backgroundable unit of work that OUTLIVES
-- the voice session and the agent loop. Aria held a conversation; when you left,
-- the work left with it. A Task persists {goal, status, transcript, artifacts,
-- blocking_ask, build_hash}; "how's X going?" reads this row, not the chat. The
-- agent loop is the ENGINE that advances a Task: status moves
-- queued -> running -> {done | needs_you | failed}. build_hash keys the work to
-- the build it ran on. A playbook is, definitionally, an ordered list of Tasks.
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    goal         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',
    transcript   TEXT NOT NULL DEFAULT '',
    artifacts    TEXT NOT NULL DEFAULT '',
    blocking_ask TEXT NOT NULL DEFAULT '',
    build_hash   TEXT,
    session_key  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, id);

-- Conversation log of record (Primitive 3). One append-only row per turn —
-- user, Aria, alert, cursor event — across every transport. This is the
-- durable source of truth the in-memory ConversationBuffer caches; it exists
-- because that buffer was memory-only and wiped on restart (forensic
-- 2026-06-16: a 72h history had to be reconstructed from session_records
-- because nothing recorded what was actually said).
CREATE TABLE IF NOT EXISTS conversation_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    role           TEXT NOT NULL,
    medium         TEXT NOT NULL,
    channel        TEXT,
    session_key    TEXT,
    parent_channel TEXT,
    text           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversation_log_session
    ON conversation_log(session_key, id);
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


# ---------------------------------------------------------------------------
# Conversation log of record (Primitive 3). The durable truth of what was said
# that the in-memory ConversationBuffer caches. Writes are telemetry-class:
# best-effort so a logging failure never breaks a live conversation, but the
# failure is logged loudly (log.warning, exc_info) — never swallowed silently.
# ---------------------------------------------------------------------------

def append_conversation_turn(
    role: str,
    medium: str,
    channel: str,
    text: str,
    session_key: str = "",
    parent_channel: str = "",
) -> None:
    """Append one turn to the durable conversation log. No-op for empty text."""
    if not (text or "").strip():
        return
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO conversation_log "
                "(ts, role, medium, channel, session_key, parent_channel, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now(), role, medium, channel, session_key, parent_channel, text),
            )
    except Exception:
        log.warning("conversation_log append failed (non-fatal)", exc_info=True)


def recent_conversation_turns(
    limit: int = 60, session_key: str | None = None
) -> list[dict[str, Any]]:
    """The most recent turns (oldest-first), optionally scoped to one thread."""
    try:
        with get_connection() as conn:
            if session_key:
                rows = conn.execute(
                    "SELECT ts, role, medium, channel, session_key, parent_channel, text "
                    "FROM conversation_log WHERE session_key = ? ORDER BY id DESC LIMIT ?",
                    (session_key, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT ts, role, medium, channel, session_key, parent_channel, text "
                    "FROM conversation_log ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in reversed(rows)]
    except Exception:
        log.warning("conversation_log read failed (non-fatal)", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Discord work-thread registry — one thread per request.
#
# A request's identity is its Discord thread, not the channel it landed in.
# `thread_id` IS the `session_key` for the agent loop, so two requests can
# never share a lock or a context window. The table is the durable record of
# which threads are Aria work threads, so a follow-up typed into an old thread
# after a restart still resolves to the same isolated session.
# ---------------------------------------------------------------------------

def bind_thread(thread_id: str, session_key: str) -> None:
    """Register `thread_id` as an Aria work thread bound to `session_key`.

    Idempotent on the thread: a second bind of the same thread keeps the
    original binding (the thread's identity never changes under it).
    """
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO discord_threads (thread_id, session_key, created_at) "
            "VALUES (?, ?, ?) ON CONFLICT(thread_id) DO NOTHING",
            (thread_id, session_key, _now()),
        )


def session_for_thread(thread_id: str) -> str | None:
    """Return the session_key bound to `thread_id`, or None if it isn't a
    known Aria work thread."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT session_key FROM discord_threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    return row["session_key"] if row else None


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
# Claude Code (Agent SDK) sessions — the durable record of threads Aria drives
# through `src/claude_code.py`. `workspace_root` enables resume after a restart
# (ClaudeAgentOptions.resume + cwd); `cost_usd` accumulates total_cost_usd.
# ---------------------------------------------------------------------------

def upsert_claude_session(
    session_id: str,
    project: str,
    workspace_root: str = "",
    status: str = "running",
) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO claude_sessions "
            "(session_id, project, workspace_root, status, started_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "status=excluded.status, "
            "workspace_root=COALESCE(NULLIF(excluded.workspace_root, ''), claude_sessions.workspace_root), "
            "project=COALESCE(NULLIF(excluded.project, ''), claude_sessions.project)",
            (session_id, project, workspace_root, status, _now()),
        )


def update_claude_session_event(
    session_id: str,
    summary: str,
    status: str | None = None,
    add_cost_usd: float | None = None,
) -> None:
    """Stamp a session's latest event. Optionally advance status and add cost."""
    sets = ["last_event_at = ?", "last_event_summary = ?"]
    params: list[Any] = [_now(), summary]
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if add_cost_usd:
        sets.append("cost_usd = cost_usd + ?")
        params.append(float(add_cost_usd))
    params.append(session_id)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE claude_sessions SET {', '.join(sets)} WHERE session_id = ?",
            params,
        )


def get_claude_session(session_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM claude_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def get_active_claude_sessions() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM claude_sessions WHERE status IN ('running', 'waiting')"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Cursor thread distillation cache (durable summaries keyed by transcript sid)
# ---------------------------------------------------------------------------

def get_thread_summary(sid: str) -> dict[str, Any] | None:
    """Return the cached distilled summary for a transcript sid, or None.

    The cache is keyed by sid; freshness is the caller's job (compare the
    stored `mtime` against the transcript's current mtime). The transcript
    JSONL on disk is the durable truth — this is only a distillation cache.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM thread_summaries WHERE sid = ?", (sid,)
        ).fetchone()
    return dict(row) if row else None


def upsert_thread_summary(
    sid: str,
    workspace_root: str,
    project_label: str,
    mtime: float,
    turns: int,
    label: str,
    purpose: str,
    did: str,
    status: str,
    open_question: str,
    model_id: str,
) -> None:
    """Insert or replace a thread's distilled summary, stamped with its mtime."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO thread_summaries "
            "(sid, workspace_root, project_label, mtime, turns, label, purpose, "
            "did, status, open_question, model_id, distilled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(sid) DO UPDATE SET "
            "workspace_root=excluded.workspace_root, "
            "project_label=excluded.project_label, mtime=excluded.mtime, "
            "turns=excluded.turns, label=excluded.label, purpose=excluded.purpose, "
            "did=excluded.did, status=excluded.status, "
            "open_question=excluded.open_question, model_id=excluded.model_id, "
            "distilled_at=excluded.distilled_at",
            (sid, workspace_root, project_label, mtime, turns, label, purpose,
             did, status, open_question, model_id, _now()),
        )


# ---------------------------------------------------------------------------
# Ground — the durable working-set. One row per role ("active_plan",
# "active_project", "last_artifact", …) binding a referent the user speaks in
# ("the plan", "that project") to a concrete artifact (path / thread / label).
#
# This is the primitive whose absence produced the honeycomb grind (forensic
# 2026-06-12): the agent loop started blind, so "live visuals three" cost ~$3
# of Opus-priced filesystem archaeology to locate, twice. Writers are the
# seams that already know the artifact (plan_with_claude, cursor_spawn,
# build_with_cursor, the set_ground tool); the single reader is
# tools._build_context, which renders ground into every loop's first message.
# ---------------------------------------------------------------------------

def set_ground(
    role: str,
    label: str,
    path: str | None = None,
    detail: str | None = None,
    source: str | None = None,
) -> None:
    """Upsert one ground binding. Raises on bad input — a writer that calls
    this with no role/label is a bug, not a condition to paper over."""
    if not role.strip() or not label.strip():
        raise ValueError("ground binding needs a non-empty role and label")
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO ground (role, label, path, detail, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(role) DO UPDATE SET "
            "label=excluded.label, path=excluded.path, detail=excluded.detail, "
            "source=excluded.source, updated_at=excluded.updated_at",
            (role.strip(), label.strip(), path, detail, source, _now()),
        )


def get_ground() -> list[dict[str, Any]]:
    """All ground bindings, most recently updated first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT role, label, path, detail, source, updated_at "
            "FROM ground ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Loop findings — what an agent loop ESTABLISHED, persisted per session_key.
#
# The other half of the honeycomb forensic: run 1 spent $6.00 locating and
# reading the answer's files, hit the cost wall, and the next run in the same
# thread re-bought the identical discovery for $5.20 because nothing carried
# over. One row per thread, replaced on every loop exit; the next loop in
# that thread injects it ("already established — do not rediscover").
# ---------------------------------------------------------------------------

def save_findings(session_key: str, findings: str, status: str) -> None:
    """Replace the findings ledger for one thread. No-op for empty input —
    an empty ledger row would only add noise to the next run's context."""
    if not session_key or not findings.strip():
        return
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO loop_findings (session_key, findings, status, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_key) DO UPDATE SET "
            "findings=excluded.findings, status=excluded.status, "
            "updated_at=excluded.updated_at",
            (session_key, findings, status, _now()),
        )


def get_findings(session_key: str) -> dict[str, Any] | None:
    """The findings ledger for one thread, or None."""
    if not session_key:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT session_key, findings, status, updated_at "
            "FROM loop_findings WHERE session_key = ?",
            (session_key,),
        ).fetchone()
    return dict(row) if row else None


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


def get_unjudged_records(hours: int = 24) -> list[dict[str, Any]]:
    """Session records from the last `hours` in a judge-worthy product that
    still have no verdict — the durable judge sweep's worklist.

    `verdicts.session_id` is the record id stringified (the judge writes
    `str(record_id)`), so the LEFT JOIN is exact and the sweep is idempotent:
    once a record has a verdict it never reappears here. No new table, no new
    store — this is the durable replacement for the fire-and-forget inline
    judge task that process churn used to drop (the longest/failed sessions
    were the ones most likely to be lost).
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    products = sorted(JUDGE_WORTHY_PRODUCTS)
    placeholders = ",".join("?" for _ in products)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT sr.id AS id, sr.product AS product, "
            "sr.session_key AS session_key, sr.timestamp AS timestamp "
            "FROM session_records sr "
            "LEFT JOIN verdicts v ON v.session_id = CAST(sr.id AS TEXT) "
            "WHERE sr.timestamp >= ? "
            f"AND sr.product IN ({placeholders}) "
            "AND v.id IS NULL "
            "ORDER BY sr.id ASC",
            (cutoff, *products),
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
            summary[p] = {"total": 0, "correct": 0, "degraded": 0, "failed": 0, "unverified": 0}
        summary[p][r["verdict"]] = r["cnt"]
        summary[p]["total"] += r["cnt"]
    for p in summary:
        total = summary[p]["total"]
        summary[p]["correctness_rate"] = summary[p]["correct"] / total if total else 0.0
    return summary


# ---------------------------------------------------------------------------
# Tasks (Primitive 1) — the durable, backgroundable unit of work.
# ---------------------------------------------------------------------------

TASK_STATUSES = ("queued", "running", "needs_you", "done", "failed")
_TASK_TERMINAL = ("done", "failed")
_TASK_ACTIVE = ("queued", "running")


def create_task(goal: str, *, session_key: str = "", build_hash: str = "") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (goal, status, build_hash, session_key, created_at, updated_at) "
            "VALUES (?, 'queued', ?, ?, ?, ?)",
            (goal, build_hash or None, session_key or None, _now(), _now()),
        )
        return int(cur.lastrowid)


def update_task(
    task_id: int,
    *,
    status: str | None = None,
    transcript: str | None = None,
    artifacts: str | None = None,
    blocking_ask: str | None = None,
) -> None:
    sets: list[str] = []
    vals: list[Any] = []
    if status is not None:
        if status not in TASK_STATUSES:
            raise ValueError(f"invalid task status {status!r} (one of {TASK_STATUSES})")
        sets.append("status = ?")
        vals.append(status)
    if transcript is not None:
        sets.append("transcript = ?")
        vals.append(transcript)
    if artifacts is not None:
        sets.append("artifacts = ?")
        vals.append(artifacts)
    if blocking_ask is not None:
        sets.append("blocking_ask = ?")
        vals.append(blocking_ask)
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.append(_now())
    vals.append(task_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)


def get_task(task_id: int) -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_tasks(*, statuses: tuple[str, ...] | None = None, limit: int = 20) -> list[dict[str, Any]]:
    q = "SELECT * FROM tasks"
    vals: list[Any] = []
    if statuses:
        q += " WHERE status IN (%s)" % ",".join("?" * len(statuses))
        vals.extend(statuses)
    q += " ORDER BY id DESC LIMIT ?"
    vals.append(limit)
    with get_connection() as conn:
        rows = conn.execute(q, vals).fetchall()
    return [dict(r) for r in rows]


def latest_task() -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def reconcile_orphaned_tasks() -> int:
    """A Task left 'running'/'queued' by a process that died (restart) can never
    advance itself again — its in-process runner is gone. Mark each as needs_you
    (interrupted), never silently leave it 'running' forever. Returns the count.
    The work itself is not auto-resumed (that could double side effects); the
    user says 'resume' and a fresh Task is started. Honest, not silent."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status IN ('running','queued')"
        ).fetchall()
        ids = [r["id"] for r in rows]
        for tid in ids:
            conn.execute(
                "UPDATE tasks SET status='needs_you', "
                "blocking_ask=?, updated_at=? WHERE id=?",
                ("I was interrupted by a restart — say 'resume <id>' and I'll start it again",
                 _now(), tid),
            )
    return len(ids)


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
