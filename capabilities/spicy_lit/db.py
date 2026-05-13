"""Outline persistence for SpicyLit. Uses the shared state.db."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from src.db import get_connection

_TABLE = """
CREATE TABLE IF NOT EXISTS spicylit_stories (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    outline     TEXT NOT NULL,
    kinks       TEXT,
    user_name   TEXT,
    created_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_table() -> None:
    with get_connection() as conn:
        conn.executescript(_TABLE)


def save_outline(
    user_id: str,
    outline: str,
    kinks: list[str] | None = None,
    user_name: str | None = None,
) -> str:
    story_id = uuid.uuid4().hex[:12]
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO spicylit_stories (id, user_id, outline, kinks, user_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (story_id, user_id, outline, json.dumps(kinks or []), user_name, _now()),
        )
    return story_id


def get_latest_outline(user_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM spicylit_stories WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "outline": row["outline"],
        "kinks": json.loads(row["kinks"]) if row["kinks"] else [],
        "user_name": row["user_name"],
        "created_at": row["created_at"],
    }
