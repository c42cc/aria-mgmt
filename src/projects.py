"""Project registry — name -> absolute path (read from projects/registry.md).

So the engine never pays to search the filesystem for a path the user named.
One home: projects/registry.md, "- name -> /absolute/path" (unicode arrow).
"""

from __future__ import annotations

import os

from .config import config

_ARROW = "\u2192"  # →


def registry() -> dict[str, str]:
    out: dict[str, str] = {}
    if not config.projects_registry.exists():
        return out
    for line in config.projects_registry.read_text().splitlines():
        line = line.strip()
        if line.startswith("- ") and _ARROW in line:
            name, path = line[2:].split(_ARROW, 1)
            out[name.strip()] = path.strip()
    return out


def resolve(name: str) -> str | None:
    """Resolve a project name/alias/absolute-path to an existing directory."""
    if not name:
        return None
    name = name.strip()
    reg = registry()
    if name in reg:
        return reg[name].rstrip("/")
    low = {k.lower(): v for k, v in reg.items()}
    if name.lower() in low:
        return low[name.lower()].rstrip("/")
    if os.path.isabs(name) and os.path.isdir(name):
        return name.rstrip("/")
    return None
