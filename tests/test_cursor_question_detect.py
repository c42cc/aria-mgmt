"""Regression: a Cursor agent that asks the user a decision via the
`AskQuestion` TOOL must be detected as a question (high severity), not a
silent `progress` turn.

The bug it fixed: question detection used to also fire on a trailing '?', which
both MISSED tool-asked decisions (the '?' is mid-prompt) and FABRICATED
decisions out of any turn that merely ended in '?'. The trailing-'?' heuristic
is now DELETED — an explicit AskQuestion/askFollowup tool call is the only
signal that a thread stopped to ask. These tests isolate that detector against
the exact tool shape Cursor emits, and prove a bare trailing '?' is progress.

Run with:
    .venv/bin/python -m unittest tests.test_cursor_question_detect -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The real assistant turn that slipped through silently (shape preserved):
# a text block whose LAST paragraph ends with a period, plus an AskQuestion
# tool_use whose prompt has a '?' mid-text.
_ASK_TURN_CONTENT = [
    {
        "type": "text",
        "text": (
            "I now have what I need to identify the one genuinely blocking "
            "unknown. Two current-state facts are confirmed and de-risk the plan."
        ),
    },
    {
        "type": "tool_use",
        "name": "AskQuestion",
        "input": {
            "questions": [
                {
                    "id": "paint_baseline",
                    "prompt": (
                        "When this initiative starts, what PAINT REALITY will "
                        "exist? This decides Move 1's closed paint_non_content "
                        "set. Per the outstanding list, the avatar Tier 2 Phase 4 "
                        "would flip style.profile='river' and set teacher.enabled: true."
                    ),
                    "options": [
                        {"id": "v1_only", "label": "v1 ship gate only"},
                        {"id": "with_avatar", "label": "Avatar Tier 2 lands first"},
                    ],
                }
            ],
            "title": "Paint baseline",
        },
    },
]


class TestAskToolDetection(unittest.TestCase):
    def test_extract_ask_question_catches_it(self):
        from src.cursor_registry import _extract_ask_question

        q = _extract_ask_question(_ASK_TURN_CONTENT)
        self.assertIsNotNone(q)
        self.assertIn("PAINT REALITY", q)

    def test_extract_ask_question_ignores_non_ask_tools(self):
        from src.cursor_registry import _extract_ask_question

        content = [
            {"type": "text", "text": "Reading a file."},
            {"type": "tool_use", "name": "Read", "input": {"path": "/tmp/x"}},
        ]
        self.assertIsNone(_extract_ask_question(content))

    def test_extract_ask_question_simple_shape(self):
        from src.cursor_registry import _extract_ask_question

        content = [
            {"type": "tool_use", "name": "ask_followup_question",
             "input": {"question": "Which database should I use?"}},
        ]
        self.assertEqual(_extract_ask_question(content), "Which database should I use?")

    def test_parse_jsonl_turns_surfaces_ask(self):
        from src.cursor_registry import _parse_jsonl_turns

        line = json.dumps({"role": "assistant", "message": {"content": _ASK_TURN_CONTENT}})
        last_assistant, last_user, plans, ask_q, saw_human = _parse_jsonl_turns([line])
        self.assertIsNotNone(ask_q)
        self.assertIn("PAINT REALITY", ask_q)
        self.assertFalse(saw_human)  # an assistant ask is not a human turn


# A turn that merely ENDS in a question mark, with NO ask-the-user tool call.
# Under the old trailing-'?' heuristic this fabricated a high-severity question
# and buzzed the user; now it must be plain progress.
_TRAILING_Q_CONTENT = [
    {"type": "text", "text": "I refactored the module and it builds. Want me to run the tests now?"},
]


class TestRegistryEmitsQuestionEvent(unittest.IsolatedAsyncioTestCase):
    """End-to-end at the registry layer: folding an SDK assistant event that
    contains an AskQuestion tool call emits a high-severity `question`."""

    async def test_sdk_assistant_askquestion_emits_question(self):
        from src.cursor_registry import CursorAgentRegistry

        reg = CursorAgentRegistry()
        events = []

        async def emit(evt):
            events.append(evt)

        reg.set_emit_callback(emit)
        await reg.register_from_sdk(
            session_id="sid-1", workspace_root="/tmp/proj_qd", instruction="x"
        )
        events.clear()  # drop the 'started' event from register_from_sdk

        await reg.record_sdk_event(
            session_id="sid-1",
            event="assistant",
            data={"message": {"content": _ASK_TURN_CONTENT}},
        )

        kinds = [(e.kind, e.severity) for e in events]
        self.assertIn(("question", "high"), kinds, f"expected a high question event, got {kinds}")
        q_evt = next(e for e in events if e.kind == "question")
        self.assertIn("PAINT REALITY", q_evt.reason)

    async def test_trailing_question_mark_is_not_a_question(self):
        """A turn that merely ENDS in '?' (no AskQuestion tool call) is progress,
        never a manufactured question — the trailing-'?' heuristic is deleted."""
        from src.cursor_registry import CursorAgentRegistry

        reg = CursorAgentRegistry()
        events = []

        async def emit(evt):
            events.append(evt)

        reg.set_emit_callback(emit)
        await reg.register_from_sdk(
            session_id="sid-2", workspace_root="/tmp/proj_noq", instruction="x"
        )
        events.clear()

        await reg.record_sdk_event(
            session_id="sid-2",
            event="assistant",
            data={"message": {"content": _TRAILING_Q_CONTENT}},
        )

        kinds = [e.kind for e in events]
        self.assertNotIn("question", kinds, f"trailing '?' must NOT be a question, got {kinds}")
        self.assertIn("progress", kinds)


_NEW_ASK_CONTENT = [
    {"type": "text", "text": "A brand new turn."},
    {"type": "tool_use", "name": "AskQuestion",
     "input": {"questions": [{"id": "n1", "prompt": "NEW live question: ship it?",
                              "options": [{"id": "y", "label": "yes"}]}]}},
]


class TestTailerDoesNotReplayBacklog(unittest.IsolatedAsyncioTestCase):
    """A tailer attaching to a transcript that already has content must NOT
    replay that backlog as fresh pings (the 'ucs is asking' stale-question
    spam). Only turns appended AFTER it attaches should notify."""

    async def test_backlog_suppressed_then_new_appends_emit(self):
        import asyncio
        import json
        import os
        import shutil
        import tempfile
        import time
        from types import SimpleNamespace
        from unittest.mock import patch
        from src.cursor_registry import CursorAgentRegistry, SessionInfo

        d = tempfile.mkdtemp()
        # The lifecycle watcher emits the question at SETTLE (not per-turn) and
        # persists a durable watermark, so this needs a fast settle + a temp db.
        fake_cfg = SimpleNamespace(
            data_dir=d, cursor_settle_seconds=0.2,
            cursor_hung_minutes=0.5, cursor_discovery_seconds=0.2,
        )
        cfg_patch = patch("src.config.config", fake_cfg)
        db_patch = patch("src.db.DB_PATH", os.path.join(d, "state.db"))
        cfg_patch.start()
        db_patch.start()
        from src.db import init_db
        init_db()

        reg = CursorAgentRegistry(tail_interval_sec=0.05)
        events = []

        async def emit(evt):
            events.append(evt)

        reg.set_emit_callback(emit)

        try:
            path = os.path.join(d, "sid.jsonl")
            # Pre-existing backlog containing a (historical) AskQuestion. Age it so
            # recency treats it as settled HISTORY (a days-old thread), not a fresh
            # hand-back — that is the case that must stay silent.
            with open(path, "w") as f:
                f.write(json.dumps({"role": "assistant", "message": {"content": _ASK_TURN_CONTENT}}) + "\n")
            _past = time.time() - 600
            os.utime(path, (_past, _past))

            agent = reg._get_or_create("/tmp/proj_backlog", source="ide")
            sess = SessionInfo(sid="sid", started_at=0.0, last_event_at=0.0, transcript_path=path)
            agent.sessions["sid"] = sess
            reg._ensure_tailer(agent, sess)

            await asyncio.sleep(0.45)  # seed over the backlog AND pass a settle window
            self.assertEqual(
                [e for e in events if e.kind == "question"], [],
                "backlog must NOT be replayed as question events",
            )

            # Append a genuinely NEW ask -> settles into exactly one question.
            with open(path, "a") as f:
                f.write(json.dumps({"role": "assistant", "message": {"content": _NEW_ASK_CONTENT}}) + "\n")
            await asyncio.sleep(0.45)

            qs = [e for e in events if e.kind == "question"]
            self.assertEqual(len(qs), 1, f"new ask should emit one question, got {len(qs)}")
            self.assertIn("ship it", qs[0].reason)
        finally:
            await reg.stop()
            db_patch.stop()
            cfg_patch.stop()
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
