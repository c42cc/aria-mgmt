"""mem0 wrapper — cross-session context and semantic memory."""

from __future__ import annotations

import logging
import os
from typing import Any

from .config import config

log = logging.getLogger(__name__)

_client: Any = None


def init_memory() -> None:
    """Initialize mem0 client with Anthropic LLM and local HuggingFace embedder."""
    global _client
    from mem0 import Memory

    data_dir = os.path.join(config.data_dir, "mem0")
    os.makedirs(data_dir, exist_ok=True)

    _client = Memory.from_config({
        "llm": {
            "provider": "anthropic",
            "config": {
                "model": config.claude_model,
                "api_key": config.anthropic_api_key,
            },
        },
        "embedder": {
            "provider": "gemini",
            "config": {
                "api_key": config.google_api_key,
                "model": "models/gemini-embedding-001",
            },
        },
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": "ucs",
                "path": data_dir,
            },
        },
    })
    log.info("mem0 initialized (Anthropic LLM + Gemini embedder)")


def remember(text: str, user_id: str = "default") -> None:
    """Store a memory. Used when the user states a durable preference."""
    if _client is None:
        raise RuntimeError("mem0 not initialized — cannot remember")
    _client.add(text, user_id=user_id)


def recall(query: str, user_id: str = "default", limit: int = 5) -> list[dict]:
    """Retrieve semantically relevant memories."""
    if _client is None:
        raise RuntimeError("mem0 not initialized — cannot recall")
    result = _client.search(query, filters={"user_id": user_id}, limit=limit)
    if isinstance(result, dict):
        return result.get("results", [])
    return result


def forget(query: str, user_id: str = "default") -> None:
    """Delete memories matching a query."""
    if _client is None:
        raise RuntimeError("mem0 not initialized — cannot forget")
    results = _client.search(query, filters={"user_id": user_id}, limit=5)
    for r in results:
        mem_id = r.get("id")
        if mem_id:
            _client.delete(mem_id)


def get_all(user_id: str = "default") -> list[dict]:
    """Retrieve all memories for a user."""
    if _client is None:
        raise RuntimeError("mem0 not initialized — cannot get_all")
    return _client.get_all(filters={"user_id": user_id})
