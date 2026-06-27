"""Step 5 lock: one typed attention gate, now driven by the lifecycle watcher.

The buzz set is unchanged policy — question / finished / errored / stalled buzz;
a user-initiated cancel and every mid-task moment are silent. What changed is the
SOURCE of the decision: a thread's surfaced state is now the single function
`_classify_terminal` over the TRANSCRIPT at settle (plus the stop-hook hint only
to tell an error/cancel from a hand-back), never a per-turn hook. These lock:

- A pending question (explicit AskQuestion) -> `question` (buzzes).
- A plan awaiting approval -> `question` (buzzes).
- The stop hook said error -> `errored`; said aborted -> `cancelled` (silent).
- A quiet thread that left a real wrap-up turn -> `finished` (buzzes).
- A quiet thread whose tail is a dangling tool_use is NOT finished; only after
  hung_window does it surface as `stalled` (the hang catcher) — never a false
  'done'.
- A Task reaching needs_you / done / failed buzzes; running / queued do not.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import bot once at collection time so the discord client it constructs caches
# its event loop now (before any IsolatedAsyncioTestCase closes one) — otherwise
# a first import inside a later test races a closed loop. Needed by the mirror
# test + TaskAttention below; the pure-logic tests use the local BUZZ instead.
from src import bot  # noqa: E402,F401

# The buzz policy, asserted locally so these pure-logic unit tests never import
# src.bot (which pulls in discord and touches the event loop at import time —
# an order-dependent flake under IsolatedAsyncioTestCase). bot._BUZZ_KINDS is
# the single home; this mirror is checked against it by test_buzz_policy_mirrors.
BUZZ = ("question", "finished", "completed", "errored", "stalled")


def _agent():
    from src.cursor_registry import CursorAgent
    return CursorAgent(
        agent_id="/tmp/aria_gate_selftest",
        workspace_root="/tmp/aria_gate_selftest",
        project_label="aria_gate_selftest",
        source="ide",
    )


def _sess(**kw):
    from src.cursor_registry import SessionInfo
    s = SessionInfo(sid="abc12345", started_at=0.0, last_event_at=0.0)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _write_jsonl(path: str, rows: list[dict]) -> None:
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _assistant_text(text: str) -> dict:
    return {"role": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _assistant_tool(name: str = "Shell") -> dict:
    return {"role": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": {}}]}}


class TerminalClassifier(unittest.TestCase):
    """The single classifier: what state did this thread hand back in?"""

    def setUp(self):
        from src import cursor_registry
        self.reg = cursor_registry.CursorAgentRegistry()

    def _classify(self, sess, quiet_for=100.0, hung=900.0):
        return self.reg._classify_terminal(_agent(), sess, quiet_for, hung)

    def test_pending_question_is_a_buzzing_question(self):
        kind, _sev, _r = self._classify(_sess(pending_question="Pick A or B?"))
        self.assertEqual(kind, "question")
        self.assertIn(kind, BUZZ)

    def test_plan_awaiting_is_a_question(self):
        kind, _sev, _r = self._classify(_sess(recent_plan_files=["/x/p.plan.md"]))
        self.assertEqual(kind, "question")

    def test_error_and_cancel_from_hook_hint(self):
        kind, _s, _r = self._classify(_sess(last_hook_status="error"))
        self.assertEqual(kind, "errored")
        self.assertIn(kind, BUZZ)
        # A user-initiated cancel is silent — never a buzz.
        kind, _s, _r = self._classify(_sess(last_hook_status="aborted"))
        self.assertEqual(kind, "cancelled")
        self.assertNotIn(kind, BUZZ)

    def test_buzz_policy_mirrors_bot(self):
        # The local BUZZ mirror must not drift from the single home in src.bot.
        from src import bot
        self.assertEqual(tuple(bot._BUZZ_KINDS), BUZZ)

    def test_handed_back_with_wrapup_is_finished(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "t.jsonl")
        _write_jsonl(p, [_assistant_text("All done — here is the summary.")])
        kind, _s, _r = self._classify(
            _sess(transcript_path=p, last_assistant_text="All done — here is the summary.")
        )
        self.assertEqual(kind, "finished")

    def test_dangling_tool_is_not_finished_then_hangs(self):
        # Quiet, but the tail is a tool_use still in flight (no wrap-up): NOT a
        # hand-back. Before hung_window -> None (wait); after -> stalled.
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "t.jsonl")
        _write_jsonl(p, [_assistant_text("working on it"), _assistant_tool("Shell")])
        sess = _sess(transcript_path=p, last_assistant_text="working on it")
        self.assertIsNone(self._classify(sess, quiet_for=10.0, hung=900.0))
        kind, _s, _r = self._classify(sess, quiet_for=1000.0, hung=900.0)
        self.assertEqual(kind, "stalled")


class TranscriptShape(unittest.TestCase):
    """`_transcript_handed_back`: a finished wrap-up vs a dangling action."""

    def _shape(self, rows):
        from src.cursor_registry import _transcript_handed_back
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "t.jsonl")
        _write_jsonl(p, rows)
        return _transcript_handed_back(p)

    def test_final_text_is_handed_back(self):
        self.assertTrue(self._shape([_assistant_tool(), _assistant_text("done")]))

    def test_final_tool_use_is_not(self):
        self.assertFalse(self._shape([_assistant_text("ok"), _assistant_tool("Shell")]))

    def test_last_entry_user_is_not(self):
        self.assertFalse(self._shape([
            _assistant_text("answer?"),
            {"role": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
        ]))

    def test_hook_status_helper(self):
        from src.cursor_registry import _hook_terminal_status
        self.assertEqual(_hook_terminal_status("stop", {"status": "completed"}), "completed")
        self.assertEqual(_hook_terminal_status("stop", {"status": "error"}), "error")
        self.assertEqual(_hook_terminal_status("stop", {"status": "aborted"}), "aborted")
        self.assertEqual(_hook_terminal_status("stop", {}), "completed")
        self.assertEqual(_hook_terminal_status("afterAgentResponse", {}), "running")


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
