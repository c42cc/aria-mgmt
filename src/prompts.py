"""Load prompt templates from disk with version control."""

from __future__ import annotations

import logging
import os
from typing import Any

from .config import config
from .db import (
    get_next_prompt_version,
    get_prompt_version_content,
    get_prompt_versions,
    insert_prompt_version,
)

log = logging.getLogger(__name__)

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


def save_template(name: str, content: str, origin: str = "user") -> str:
    """Write content to a template file. Archives the old version first."""
    path = get_path(name)
    if os.path.exists(path):
        _archive_current(name, origin)
    with open(path, "w") as f:
        f.write(content)
    _cache.pop(name, None)
    return path


def _archive_current(name: str, origin: str) -> None:
    """Archive the current file content as a new version in the DB."""
    path = get_path(name)
    if not os.path.exists(path):
        return
    with open(path) as f:
        current_content = f.read()
    version = get_next_prompt_version(name)
    archive_origin = "initial" if version == 1 else origin
    insert_prompt_version(name, version, current_content, origin=archive_origin)
    log.info("Archived %s v%d (origin=%s, %d chars)", name, version, archive_origin, len(current_content))


def get_versions(name: str) -> list[dict[str, Any]]:
    """Return version history for a prompt from the DB."""
    return get_prompt_versions(name)


def rollback_template(name: str, version: int) -> str:
    """Restore a previous version from the DB to the file.

    Archives the current content as origin='rollback' before overwriting.
    """
    content = get_prompt_version_content(name, version)
    if content is None:
        raise ValueError(f"Prompt '{name}' version {version} not found")
    save_template(name, content, origin="rollback")
    log.info("Rolled back %s to v%d", name, version)
    return content


def get_path(name: str) -> str:
    """Return the absolute file path for a template name."""
    return os.path.join(config.prompts_dir, f"{name}.md")


def clear_cache() -> None:
    _cache.clear()
