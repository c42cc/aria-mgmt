"""mem0 wrapper — cross-session context and semantic memory."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_client: Any = None


def init_memory() -> None:
    """Initialize mem0 client. Call once at bot startup."""
    global _client
    try:
        from mem0 import Memory
        _client = Memory()
        log.info("mem0 initialized")
    except Exception:
        log.exception("Failed to initialize mem0 — memory features disabled")


def remember(text: str, user_id: str = "default") -> None:
    """Store a memory. Used when the user states a durable preference."""
    if _client is None:
        return
    try:
        _client.add(text, user_id=user_id)
    except Exception:
        log.exception("mem0 add failed")


def recall(query: str, user_id: str = "default", limit: int = 5) -> list[dict]:
    """Retrieve semantically relevant memories."""
    if _client is None:
        return []
    try:
        return _client.search(query, user_id=user_id, limit=limit)
    except Exception:
        log.exception("mem0 search failed")
        return []


def get_all(user_id: str = "default") -> list[dict]:
    """Retrieve all memories for a user."""
    if _client is None:
        return []
    try:
        return _client.get_all(user_id=user_id)
    except Exception:
        log.exception("mem0 get_all failed")
        return []
