"""Loops — capability as data.

A Loop is one file in loops/*.yaml. It declares the questions that must be
answered (required_slots), how the filled answers become a concrete instruction
to the engine (dispatch), which endpoint runs it, and what "done" means. The
conductor is a generic interpreter of this schema — adding a capability is
adding a file, never growing the core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import config


@dataclass(frozen=True)
class Slot:
    key: str
    ask: str


@dataclass(frozen=True)
class Loop:
    id: str
    name: str
    description: str
    endpoint: str
    loop: str
    dispatch: str
    done: str
    required_slots: list[Slot] = field(default_factory=list)
    optional_slots: list[Slot] = field(default_factory=list)
    gates: list[str] = field(default_factory=list)
    report: str = "text"

    def required_keys(self) -> list[str]:
        return [s.key for s in self.required_slots]


_REQUIRED_FIELDS = ("id", "name", "description", "endpoint", "loop", "dispatch", "done")


def _parse(path: Path) -> Loop:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"loop {path.name}: top level must be a mapping")
    missing = [f for f in _REQUIRED_FIELDS if not raw.get(f)]
    if missing:
        raise ValueError(f"loop {path.name}: missing required field(s) {missing}")

    def slots(key: str) -> list[Slot]:
        out = []
        for item in raw.get(key, []) or []:
            if not item.get("key") or not item.get("ask"):
                raise ValueError(f"loop {path.name}: every {key} entry needs key + ask")
            out.append(Slot(key=item["key"], ask=item["ask"]))
        return out

    return Loop(
        id=raw["id"],
        name=raw["name"],
        description=raw["description"],
        endpoint=raw["endpoint"],
        loop=raw["loop"].strip(),
        dispatch=raw["dispatch"].strip(),
        done=raw["done"].strip(),
        required_slots=slots("required_slots"),
        optional_slots=slots("optional_slots"),
        gates=list(raw.get("gates", []) or []),
        report=raw.get("report", "text"),
    )


def load_loops() -> dict[str, Loop]:
    """Load + validate every loop. Loud on a malformed or duplicate-id file."""
    loops: dict[str, Loop] = {}
    if not config.loops_dir.is_dir():
        raise FileNotFoundError(f"loops dir not found: {config.loops_dir}")
    for path in sorted(config.loops_dir.glob("*.yaml")):
        loop = _parse(path)
        if loop.id in loops:
            raise ValueError(f"duplicate loop id {loop.id!r} ({path.name})")
        loops[loop.id] = loop
    if not loops:
        raise FileNotFoundError(f"no loops found in {config.loops_dir}")
    return loops
