"""Load prompt templates from disk."""

from __future__ import annotations

import os

from .config import config

_cache: dict[str, str] = {}


def load_template(name: str) -> str:
    """Load a prompt template by name (without .md extension).

    Falls back to 'planning' if the named template doesn't exist.
    Templates are cached after first read.
    """
    if name in _cache:
        return _cache[name]

    path = os.path.join(config.prompts_dir, f"{name}.md")
    if not os.path.exists(path):
        path = os.path.join(config.prompts_dir, "planning.md")

    with open(path) as f:
        content = f.read()

    _cache[name] = content
    return content


def clear_cache() -> None:
    _cache.clear()
