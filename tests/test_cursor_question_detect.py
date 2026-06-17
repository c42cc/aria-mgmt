"""Regression: a Cursor agent that asks the user a decision via the
`AskQuestion` TOOL must be detected as a question (high severity), not a
silent `progress` turn.

The bug: question detection only read assistant `text` blocks and only fired on
a trailing '?'. Agents ask decisions by CALLING `AskQuestion` (the '?' is
mid-prompt, followed by declarative sentences), so the real decision was
invisible and Aria never pinged — "why the fuck did aria miss this question?"

These tests isolate the detector against the exact tool shape Cursor emits.

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
    def test_prose_heuristic_misses_it(self):
        from src.cursor_registry import _question_in_text

        text = _ASK_TURN_CONTENT[0]["text"]
        self.assertIsNone(_question_in_text(text), "prose heuristic should miss tool-asked questions")

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
        last_assistant, last_user, plans, ask_q = _parse_jsonl_turns([line])
        self.assertIsNotNone(ask_q)
        self.assertIn("PAINT REALITY", ask_q)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
