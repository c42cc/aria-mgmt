"""Prompt templates with `{{include:NAME}}` resolution.

This is the preserved doctrine-injection loop: a persona references the shared
doctrine with `{{include:_principles}}` instead of pasting it, so the doctrine
has exactly one home (prompts/_principles.md) and cannot drift. A missing
include or a cycle raises LOUDLY — a half-rendered prompt is a silent failure.

Unlike v1, the resolved text is ALSO what reaches Aria's engine: the conductor
builds the Claude Code instruction from an `{{include:_principles}}` persona and
writes a CLAUDE.md into the workspace (review 3.6), so the doctrine reaches the
engine, not just the build-time IDE agent.
"""

from __future__ import annotations

import re

from .config import config

_INCLUDE_RE = re.compile(r"\{\{include:([A-Za-z0-9_\-]+)\}\}")


def _path(name: str):
    return config.prompts_dir / f"{name}.md"


def load(name: str) -> str:
    """Load a template by name (no .md), resolving includes recursively."""
    return _resolve(name, ())


def _resolve(name: str, seen: tuple[str, ...]) -> str:
    if name in seen:
        raise ValueError(f"prompt include cycle: {' -> '.join((*seen, name))}")
    path = _path(name)
    if not path.exists():
        raise FileNotFoundError(f"prompt '{name}' not found at {path}")
    text = path.read_text()
    return _INCLUDE_RE.sub(lambda m: _resolve(m.group(1), (*seen, name)), text)


def names() -> list[str]:
    return sorted(p.stem for p in config.prompts_dir.glob("*.md"))
