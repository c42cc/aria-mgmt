"""Step 4 lock: the Task primitive — a durable, backgroundable unit of work that
outlives the voice session and the agent loop.

Drives the lifecycle with a STUB engine (no live Claude), proving:
  - create -> running -> done / needs_you / failed transitions,
  - a wall result parks the Task as needs_you carrying the one ask,
  - an engine crash marks the Task failed (loud), never silently stuck running,
  - an orphaned 'running' Task is reconciled to needs_you on boot.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TaskPrimitive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.path = os.path.join(self._tmp, "state.db")
        self._patcher = patch("src.db.DB_PATH", self.path)
        self._patcher.start()
        from src.db import init_db
        init_db()

    def tearDown(self):
        self._patcher.stop()

    def test_classify_engine_result_maps_wall_to_needs_you(self):
        from src import tasks
        blocker = (
            "**Blocked — I hit a wall and stopped instead of grinding.**\n"
            "What failed: authentication failed\n"
            "What I need to proceed: a credential.\n"
        )
        status, transcript, ask = tasks.classify_engine_result(blocker)
        self.assertEqual(status, "needs_you")
        self.assertIn("credential", ask.lower())
        self.assertTrue(transcript.startswith("**Blocked"))

    def test_classify_engine_result_maps_normal_to_done(self):
        from src import tasks
        status, transcript, ask = tasks.classify_engine_result("Here are your 3 emails.")
        self.assertEqual(status, "done")
        self.assertEqual(ask, "")

    def test_advance_task_done(self):
        from src import tasks
        from src.db import create_task, get_task

        async def engine(goal, sk):
            return f"did it: {goal}"

        tid = create_task("summarize my mail")
        asyncio.run(tasks.advance_task(tid, engine))
        t = get_task(tid)
        self.assertEqual(t["status"], "done")
        self.assertIn("did it", t["transcript"])

    def test_advance_task_needs_you(self):
        from src import tasks
        from src.db import create_task, get_task

        async def engine(goal, sk):
            return (
                "**Blocked — I hit a wall and stopped instead of grinding.**\n"
                "What failed: no access\nWhat I need to proceed: the token.\n"
            )

        tid = create_task("deploy the thing")
        asyncio.run(tasks.advance_task(tid, engine))
        t = get_task(tid)
        self.assertEqual(t["status"], "needs_you")
        self.assertIn("token", t["blocking_ask"].lower())

    def test_advance_task_failed_is_loud(self):
        from src import tasks
        from src.db import create_task, get_task

        async def engine(goal, sk):
            raise RuntimeError("boom")

        tid = create_task("do the impossible")
        with self.assertRaises(RuntimeError):
            asyncio.run(tasks.advance_task(tid, engine))
        t = get_task(tid)
        self.assertEqual(t["status"], "failed")
        self.assertIn("boom", t["transcript"])

    def test_reconcile_orphaned_running_task(self):
        from src import tasks
        from src.db import create_task, update_task, get_task

        tid = create_task("long job")
        update_task(tid, status="running")
        n = tasks.reconcile_orphaned_on_boot()
        self.assertGreaterEqual(n, 1)
        t = get_task(tid)
        self.assertEqual(t["status"], "needs_you")
        self.assertIn("restart", t["blocking_ask"].lower())

    def test_list_tasks_filters_by_status(self):
        from src.db import create_task, update_task, list_tasks

        a = create_task("a")
        b = create_task("b")
        update_task(b, status="done")
        active = list_tasks(statuses=("queued", "running", "needs_you"))
        ids = {t["id"] for t in active}
        self.assertIn(a, ids)
        self.assertNotIn(b, ids)


if __name__ == "__main__":
    unittest.main()
