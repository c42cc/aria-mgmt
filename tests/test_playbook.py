"""Step 7 lock: a playbook is an ordered list of Tasks.

Proves the step parser (only list items are goals) and the runner's sequencing:
all-done runs every step in order; a step that hits a wall HALTS the playbook at
that step (the chief-of-staff "here's the one thing I need"), it does not grind on.
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

from src import playbook  # noqa: E402


class ParseSteps(unittest.TestCase):
    def test_only_list_items_become_steps(self):
        text = (
            "# Title\n\nSome prose that is not a step.\n\n"
            "1. first goal\n"
            "2) second goal\n"
            "- third goal\n"
            "* fourth goal\n\n"
            "More prose.\n"
        )
        steps = playbook.parse_steps(text)
        self.assertEqual(steps, ["first goal", "second goal", "third goal", "fourth goal"])

    def test_example_playbook_loads(self):
        steps = playbook.load_playbook("example")
        self.assertGreaterEqual(len(steps), 3)
        self.assertTrue(all(isinstance(s, str) and s for s in steps))


class RunPlaybook(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patcher = patch("src.db.DB_PATH", os.path.join(self._tmp, "state.db"))
        self._patcher.start()
        from src.db import init_db
        init_db()
        # A throwaway playbook in a temp workflows dir.
        self._wf = tempfile.mkdtemp()
        with open(os.path.join(self._wf, "t.playbook.md"), "w") as f:
            f.write("# t\n\n1. step one\n2. step two\n3. step three\n")
        self._wf_patch = patch.object(playbook, "playbooks_dir", lambda: self._wf)
        self._wf_patch.start()

    def tearDown(self):
        self._wf_patch.stop()
        self._patcher.stop()

    def test_all_done_runs_every_step_in_order(self):
        seen = []

        async def engine(goal, sk):
            seen.append(goal)
            return f"done: {goal}"

        summary = asyncio.run(playbook.run_playbook("t", engine))
        self.assertEqual(summary["status"], "done")
        self.assertEqual(summary["total"], 3)
        self.assertEqual(seen, ["step one", "step two", "step three"])

    def test_halts_on_wall(self):
        async def engine(goal, sk):
            if goal == "step two":
                return (
                    "**Blocked — I hit a wall and stopped instead of grinding.**\n"
                    "What failed: no access\nWhat I need to proceed: the token.\n"
                )
            return f"done: {goal}"

        summary = asyncio.run(playbook.run_playbook("t", engine))
        self.assertEqual(summary["status"], "halted")
        self.assertEqual(summary["halted_at"], 2)
        self.assertIn("token", summary["reason"].lower())
        # step three never ran
        self.assertEqual(len(summary["steps"]), 2)


if __name__ == "__main__":
    unittest.main()
