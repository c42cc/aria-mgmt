"""Step 5 lock: one typed attention gate.

- The buzz allow-set is a single policy: every STOP buzzes — a `question`, a
  `finished`/`completed`, an `errored`, or a `stalled` — each with a factual
  summary. Routine 'progress', a 'started', and a user-initiated 'cancelled'
  are silent-audit-only.
- A user-initiated CANCEL is a silent 'cancelled' kind, never a buzz (the
  explicit "stop bugging me on cancel" complaint).
- A constructed plan is a 'question' (decision) and DOES buzz.
- A completion/error buzzes with a summary, but is NEVER turned into a
  fabricated question or a Claude-invented "next step" (those sources are gone).
- A Task reaching needs_you / done / failed buzzes; running / queued do not.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _agent():
    from src.cursor_registry import CursorAgent
    return CursorAgent(
        agent_id="/tmp/aria_gate_selftest",
        workspace_root="/tmp/aria_gate_selftest",
        project_label="aria_gate_selftest",
        source="ide",
    )


class ClassifyHook(unittest.TestCase):
    def test_user_cancel_is_silent(self):
        from src.cursor_registry import _classify_hook
        from src.bot import _BUZZ_KINDS
        for status in ("aborted", "cancelled", "canceled", "stopped"):
            kind, sev, _reason = _classify_hook("stop", {"status": status}, _agent())
            self.assertEqual(kind, "cancelled", status)
            self.assertNotIn(kind, _BUZZ_KINDS, status)

    def test_completion_is_silent_but_error_buzzes(self):
        # A completion is still classified as `finished` but no longer buzzes
        # (Corbin's rule 2026-06-20: a thread merely finishing is silent-audit
        # only — notify on a question or a 15-min stall, not "done"). An error
        # is a loud STOP and still buzzes.
        from src.cursor_registry import _classify_hook
        from src.bot import _BUZZ_KINDS
        kind, _s, _r = _classify_hook("stop", {"status": "completed"}, _agent())
        self.assertEqual(kind, "finished")
        self.assertNotIn(kind, _BUZZ_KINDS)
        kind, _s, _r = _classify_hook("stop", {"status": "error"}, _agent())
        self.assertEqual(kind, "errored")
        self.assertIn(kind, _BUZZ_KINDS)

    def test_constructed_plan_is_a_question(self):
        from src.cursor_registry import _classify_hook
        from src.bot import _BUZZ_KINDS
        kind, sev, _r = _classify_hook(
            "postToolUse", {"tool_name": "create_plan"}, _agent()
        )
        self.assertEqual(kind, "question")
        self.assertIn(kind, _BUZZ_KINDS)


class TaskAttention(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patcher = patch("src.db.DB_PATH", os.path.join(self._tmp, "state.db"))
        self._patcher.start()
        from src.db import init_db
        init_db()

    def tearDown(self):
        self._patcher.stop()

    async def test_needs_you_buzzes_running_does_not(self):
        from src import bot
        from src.db import create_task, update_task

        tid = create_task("a background job")

        with patch.object(bot, "_notify_user_buzz", AsyncMock(return_value=True)) as buzz:
            update_task(tid, status="running")
            await bot._notify_task_event(tid)
            buzz.assert_not_awaited()

            update_task(tid, status="needs_you", blocking_ask="the token")
            await bot._notify_task_event(tid)
            buzz.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
