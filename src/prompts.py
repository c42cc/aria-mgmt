"""Load prompt templates from disk."""

from __future__ import annotations

import os

from .config import config

_cache: dict[str, str] = {}


def load_template(name: str) -> str:
    """Load a prompt template by name (without .md extension).

    Templates are cached after first read.
    """
    if name in _cache:
        return _cache[name]

    path = get_path(name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Prompt template '{name}' not found at {path}")

    with open(path) as f:
        content = f.read()

    _cache[name] = content
    return content


def list_templates() -> list[str]:
    """Return sorted names of all prompt templates (without .md extension)."""
    names = []
    for f in os.listdir(config.prompts_dir):
        if f.endswith(".md"):
            names.append(f[:-3])
    return sorted(names)


def read_raw(name: str) -> str:
    """Read a template fresh from disk, bypassing cache."""
    path = get_path(name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Prompt template '{name}' not found at {path}")
    with open(path) as f:
        return f.read()


def save_template(name: str, content: str) -> str:
    """Write content to a template file and invalidate its cache entry."""
    path = get_path(name)
    with open(path, "w") as f:
        f.write(content)
    _cache.pop(name, None)
    return path


def get_path(name: str) -> str:
    """Return the absolute file path for a template name."""
    return os.path.join(config.prompts_dir, f"{name}.md")


def clear_cache() -> None:
    _cache.clear()
