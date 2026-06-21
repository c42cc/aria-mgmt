"""The durable conversation — Aria's memory, one home.

Every turn (the user's, Aria's, an engine observation) is persisted here, so Aria
always has the right context: her last messages, the full history, across
sessions AND threads. The conductor loads recent turns as its context each turn
— the model gets the raw history as DATA and does the understanding itself
(Software 2.0). There is no retrieval pipeline, no summarizer, no vector store to
drift; the transcript IS the memory.

This store is also the single home for the per-turn metrics (latency, phase,
cost), so "the conversation" is not duplicated across a transcript here and a
telemetry trace there. One table, indexed by time and thread.

Roles persisted: `user`, `aria`, `observation` (an engine result folded back in).
For the Anthropic API, `aria` -> assistant and everything else -> user, with
consecutive same-role turns merged so the message sequence is always valid.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .config import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    iso        TEXT    NOT NULL,
    thread     TEXT    NOT NULL,
    session    TEXT    NOT NULL,
    channel    TEXT    NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    phase      TEXT,
    latency_ms INTEGER,
    loop_id    TEXT,
    cost_usd   REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread, id);
"""


def _conn() -> sqlite3.Connection:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.conversation_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def append(
    *,
    thread: str,
    session: str,
    channel: str,
    role: str,
    content: str,
    phase: str | None = None,
    latency_ms: int | None = None,
    loop_id: str | None = None,
    cost_usd: float | None = None,
) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages(ts,iso,thread,session,channel,role,content,phase,latency_ms,loop_id,cost_usd)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                thread, session, channel, role, content, phase, latency_ms, loop_id, cost_usd,
            ),
        )
        return int(cur.lastrowid)


def _recent_rows(thread: str | None, limit: int) -> list[sqlite3.Row]:
    with _conn() as conn:
        if thread is not None:
            rows = conn.execute(
                "SELECT * FROM messages WHERE thread=? ORDER BY id DESC LIMIT ?", (thread, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return list(reversed(rows))  # chronological


def thread_messages(thread: str, limit: int | None = None) -> list[dict]:
    """Recent turns of `thread` as a valid alternating user/assistant sequence
    for the Anthropic API (the conductor's context)."""
    rows = _recent_rows(thread, limit or config.context_window_turns)
    msgs: list[dict] = []
    for r in rows:
        role = "assistant" if r["role"] == "aria" else "user"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + r["content"]
        else:
            msgs.append({"role": role, "content": r["content"]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)  # the API requires the first message to be 'user'
    return msgs


def threads() -> list[str]:
    with _conn() as conn:
        return [r["thread"] for r in conn.execute(
            "SELECT thread, MAX(id) m FROM messages GROUP BY thread ORDER BY m DESC"
        ).fetchall()]


def other_threads_context(current_thread: str, per_thread: int = 6, max_threads: int = 4) -> str:
    """Raw recent turns from OTHER threads, tagged, for the system prompt — so
    Aria has multi-thread access (she sees what's going on elsewhere) without
    polluting the current thread's message sequence. Raw data, not a summary."""
    others = [t for t in threads() if t != current_thread][:max_threads]
    if not others:
        return ""
    blocks: list[str] = []
    for t in others:
        rows = _recent_rows(t, per_thread)
        if not rows:
            continue
        lines = [f"  [{_ago(r['ts'])}] {r['role']}: {r['content'][:160]}" for r in rows]
        blocks.append(f"- thread '{t}':\n" + "\n".join(lines))
    return "\n".join(blocks)


def latencies() -> list[int]:
    with _conn() as conn:
        return [r["latency_ms"] for r in conn.execute(
            "SELECT latency_ms FROM messages WHERE role='aria' AND latency_ms IS NOT NULL AND latency_ms>0"
        ).fetchall()]


def _ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"
