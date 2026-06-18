"""src.playbook — the payoff: a playbook is an ordered list of Tasks.

Once Tasks are durable (Primitive 1), outcomes honest (Primitive 2), truth
verified (Primitive 3), and attention gated (Primitive 4), a playbook is just an
ordered list of Tasks: name it, give a few commands, walk away. It is built LAST
because it is only reliable once 0-6 hold.

Stored as editable markdown in workflows/<name>.playbook.md — one ordered-list
item per Task goal. Behavior is data, like prompts: edit the file, the playbook
changes. The runner creates one Task per step and advances them IN ORDER,
halting on the first step that ends needs_you / failed (the chief-of-staff
"here's the one thing I need"), so you are asked once, not buried.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Awaitable, Callable, Optional

from . import db, tasks

log = logging.getLogger(__name__)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SUFFIX = ".playbook.md"
# An ordered-list item: "1. goal", "1) goal", "- goal", "* goal".
_STEP_RE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(.+?)\s*$")

_RUNNING: dict[str, asyncio.Task] = {}


def playbooks_dir() -> str:
    return os.path.join(_REPO, "workflows")


def list_playbooks() -> list[str]:
    d = playbooks_dir()
    if not os.path.isdir(d):
        return []
    return sorted(f[: -len(_SUFFIX)] for f in os.listdir(d) if f.endswith(_SUFFIX))


def _path(name: str) -> str:
    safe = os.path.basename(name or "").strip()
    if safe.endswith(_SUFFIX):
        safe = safe[: -len(_SUFFIX)]
    return os.path.join(playbooks_dir(), safe + _SUFFIX)


def parse_steps(text: str) -> list[str]:
    """The ordered-list items, in order. Headings, prose, and blanks are skipped
    — only list items are Task goals."""
    steps: list[str] = []
    for line in text.splitlines():
        m = _STEP_RE.match(line)
        if m:
            steps.append(m.group(1).strip())
    return steps


def load_playbook(name: str) -> list[str]:
    path = _path(name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"no playbook {name!r} (looked for {path})")
    with open(path) as f:
        return parse_steps(f.read())


async def run_playbook(name: str, engine: tasks.Engine) -> dict:
    """Run a playbook's steps as an ordered sequence of Tasks. Halts on the first
    step that ends needs_you / failed. Returns a summary."""
    steps = load_playbook(name)
    if not steps:
        raise ValueError(f"playbook {name!r} has no steps")
    results: list[dict] = []
    for i, goal in enumerate(steps, start=1):
        tid = db.create_task(goal, session_key=f"playbook:{name}:{i}")
        t = await tasks.advance_task(tid, engine)
        results.append({"step": i, "task_id": tid, "goal": goal, "status": t["status"]})
        if t["status"] in ("needs_you", "failed"):
            return {
                "name": name,
                "status": "halted",
                "halted_at": i,
                "reason": t.get("blocking_ask") or "",
                "total": len(steps),
                "steps": results,
            }
    return {"name": name, "status": "done", "total": len(steps), "steps": results}


PlaybookFinish = Callable[[dict], Awaitable[None]]


def start_playbook(
    name: str, engine: tasks.Engine, *, on_finish: Optional[PlaybookFinish] = None
) -> int:
    """Run a playbook in the BACKGROUND (so the user walks away). Validates that
    the playbook exists + has steps now (raises if not); returns the step count.
    Each step's Task buzzes on its own via the Task notifier; on_finish fires once
    when the whole playbook ends (done or halted)."""
    steps = load_playbook(name)
    if not steps:
        raise ValueError(f"playbook {name!r} has no steps")

    async def _runner() -> None:
        try:
            summary = await run_playbook(name, engine)
            if on_finish is not None:
                await on_finish(summary)
        except Exception:
            log.exception("playbook %s runner failed", name)
        finally:
            _RUNNING.pop(name, None)

    _RUNNING[name] = asyncio.create_task(_runner())
    return len(steps)
