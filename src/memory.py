"""Durable facts — the UX lever, not just storage.

Memory exists so Aria never asks what she already knows: durable facts pre-fill
loop slots and let the conductor SKIP questions whose answers are settled
(which repo you always mean, your default branch, which room is "the living
room"). Three questions the first time, one the tenth (review 2.5).

A flat JSON map of fact -> value. One file, one home. No ORM, no vector store
in Phase 0 — add that only when recall over many facts earns it.
"""

from __future__ import annotations

import json

from .config import config


def _load() -> dict[str, str]:
    p = config.memory_path
    if not p.exists():
        return {}
    return json.loads(p.read_text() or "{}")


def all_facts() -> dict[str, str]:
    return _load()


def remember(key: str, value: str) -> None:
    facts = _load()
    facts[key.strip()] = value.strip()
    config.memory_path.parent.mkdir(parents=True, exist_ok=True)
    config.memory_path.write_text(json.dumps(facts, indent=2, sort_keys=True))


def render_for_prompt() -> str:
    """Render facts for the conductor's system prompt, or a clear 'none yet'."""
    facts = _load()
    if not facts:
        return "(no durable facts known yet)"
    return "\n".join(f"- {k}: {v}" for k, v in sorted(facts.items()))
