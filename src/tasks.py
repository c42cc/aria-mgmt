"""src.tasks — the Task primitive: a durable, backgroundable unit of work.

The one genuinely-new primitive from the dysfunction post-mortem. Aria held a
*conversation*; when you left your desk, the conversation was gone and so was
the work. A Task is a persisted object — {goal, status, transcript, artifacts,
blocking_ask, build_hash} (src/db.py `tasks`) — that:

  - outlives any voice session and the bounded agent loop,
  - has a goal and a status (queued -> running -> {done | needs_you | failed}),
  - runs in the background (an asyncio task in the long-running bot process, so
    it survives voice-session churn),
  - is checked by READING the object ("how's the backup?"), not the chat.

The agent loop (tools._do_with_claude) stops being THE unit of work and becomes
the ENGINE that advances a Task. A playbook is, definitionally, an ordered list
of Tasks — which is exactly why this primitive is the precondition for "name a
playbook, give a few commands, and walk away."
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from . import build_hash as _bh
from . import db

log = logging.getLogger(__name__)

# The engine signature: advance the work for `goal` under `session_key`, return
# the result string (the same contract as tools._do_with_claude). Injected so
# tests can drive the lifecycle with a stub instead of a live Claude loop.
Engine = Callable[[str, str], Awaitable[str]]

# Background asyncio tasks, held so they are not garbage-collected and so they
# survive voice-session churn (they live in the long-running bot process).
_RUNNING: dict[int, asyncio.Task] = {}

# Optional notifier: buzz when a Task reaches needs_you / done / failed. Wired by
# bot.py's attention gate (Step 5). None in tests / standalone — never required.
TaskNotifier = Callable[[int], Awaitable[None]]
_notifier: Optional[TaskNotifier] = None


def set_notifier(fn: Optional[TaskNotifier]) -> None:
    global _notifier
    _notifier = fn


def classify_engine_result(result: str) -> tuple[str, str, str]:
    """Map an engine result -> (status, transcript, blocking_ask). Pure.

    The loop renders a wall as a '**Blocked …' message that already carries the
    partial work + the one ask (src/outcomes.format_block + _blocker_with_findings).
    That becomes `needs_you` (the work is parked on the user's one input), with the
    ask lifted out. Anything else is `done`.
    """
    text = (result or "").strip()
    if text.lstrip().startswith("**Blocked"):
        return "needs_you", text, _first_ask(text)
    if not text:
        return "done", "(the engine produced no output)", ""
    return "done", text, ""


def _first_ask(blocker_text: str) -> str:
    """Lift the single ask out of a '**Blocked …' message."""
    for line in blocker_text.splitlines():
        low = line.lower()
        if low.startswith("what i need"):
            return line.split(":", 1)[-1].strip() if ":" in line else line.strip()
    return "your input to proceed"


async def _notify(task_id: int) -> None:
    if _notifier is None:
        return
    try:
        await _notifier(task_id)
    except Exception:
        log.exception("task notifier raised for task %d", task_id)


async def advance_task(task_id: int, engine: Engine) -> dict:
    """Run the engine on the Task's goal and record the outcome.

    Loud: an engine exception marks the Task `failed` (with the error in the
    transcript) and re-raises — never a silent stuck 'running'.
    """
    task = db.get_task(task_id)
    if not task:
        raise ValueError(f"no task {task_id}")

    db.update_task(task_id, status="running")
    session_key = f"task:{task_id}"
    try:
        result = await engine(task["goal"], session_key)
    except Exception as exc:
        log.exception("task %d engine raised", task_id)
        db.update_task(
            task_id,
            status="failed",
            transcript=f"engine error: {type(exc).__name__}: {exc}",
            blocking_ask="the error above — tell me how you'd like to proceed",
        )
        await _notify(task_id)
        raise

    status, transcript, ask = classify_engine_result(result)
    db.update_task(task_id, status=status, transcript=transcript, blocking_ask=ask)
    await _notify(task_id)
    return db.get_task(task_id)  # type: ignore[return-value]


def start_task(goal: str, engine: Engine, *, session_key: str = "") -> int:
    """Create a Task and advance it in the BACKGROUND. Returns the id at once.

    The runner is an asyncio task in the long-running bot process, so the work
    outlives the voice session that asked for it — the whole point of the
    primitive ("walk away"). Requires a running event loop.
    """
    task_id = db.create_task(
        goal, session_key=session_key, build_hash=_bh.compute_build_hash()
    )

    async def _runner() -> None:
        try:
            await advance_task(task_id, engine)
        except Exception:
            # advance_task already recorded `failed` + notified; swallow here so
            # the background runner doesn't raise into the event loop's
            # exception handler (the failure is durable on the Task row).
            log.debug("task %d background runner ended on failure", task_id)
        finally:
            _RUNNING.pop(task_id, None)

    _RUNNING[task_id] = asyncio.create_task(_runner())
    return task_id


def reconcile_orphaned_on_boot() -> int:
    """At startup, mark any Task a dead process left mid-flight as needs_you
    (interrupted) — never silently stuck 'running'. Returns the count."""
    n = db.reconcile_orphaned_tasks()
    if n:
        log.warning("tasks: marked %d orphaned task(s) needs_you (restart interrupted them)", n)
    return n


def task_summary(task: dict) -> str:
    """A one-glance human line for "how's X going?"."""
    tid = task["id"]
    status = task["status"]
    goal = (task["goal"] or "").strip().replace("\n", " ")
    head = goal[:80] + ("…" if len(goal) > 80 else "")
    if status == "needs_you":
        return f"Task #{tid} [{status}]: {head} — I need: {task.get('blocking_ask') or 'your input'}"
    if status == "done":
        out = (task.get("transcript") or "").strip().replace("\n", " ")
        return f"Task #{tid} [done]: {head} — {out[:140]}" if out else f"Task #{tid} [done]: {head}"
    if status == "failed":
        return f"Task #{tid} [FAILED]: {head} — {(task.get('transcript') or '')[:140]}"
    return f"Task #{tid} [{status}]: {head}"
