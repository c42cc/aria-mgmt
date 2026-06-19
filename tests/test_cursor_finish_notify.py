"""Regression: the cursor-watch off-voice delivery contract.

A watched thread buzzes the user OFF-VOICE only when it genuinely STOPPED TO ASK
— the `question` kind (an explicit AskQuestion/askFollowup tool call, or a plan
awaiting approval). A thread that merely finishes, errors, or stalls is recorded
to the forced-silent `#ucs-alerts` audit stream and NEVER manufactured into a
buzz. This is the 2026-06-19 collapse of the fabricated-question firehose: it
killed the "finished -> invent a next step" auto-proposal and the trailing-'?'
prose heuristic that buzzed the user about live_visuals_4/ucs "questions" that
were never asked.

These tests isolate `_narrate_registry_event`'s off-voice path and prove a
question buzzes while finished/errored/stalled/progress/started stay silent.

Run with:
    .venv/bin/python -m unittest tests.test_cursor_finish_notify -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_event(kind: str, severity: str = "low", *, reason: str = "", question: str | None = None):
    from src.cursor_registry import CursorAgent, RegistryEvent

    agent = CursorAgent(
        agent_id="/tmp/aria_notify_selftest",
        workspace_root="/tmp/aria_notify_selftest",
        project_label="aria_notify_selftest",
        source="ide",
    )
    agent.last_assistant_text = "Did the thing."
    agent.last_event_at = 1234.0
    if question:
        agent.pending_question = question
    return RegistryEvent(
        kind=kind,
        agent=agent,
        severity=severity,
        reason=reason or f"Cursor {kind} in aria_notify_selftest.",
    )


class TestNotifyUserBuzz(unittest.IsolatedAsyncioTestCase):
    """`_notify_user_buzz` prefers a DM, falls back to a non-silent #ucs ping."""

    async def test_prefers_dm(self):
        from src import bot

        with patch.object(bot, "_dm_authorized_user", AsyncMock(return_value=True)) as dm, \
             patch.object(bot, "post_to_text", AsyncMock()) as ucs:
            ok = await bot._notify_user_buzz("buzz <@1>")
            self.assertTrue(ok)
            dm.assert_awaited_once()
            ucs.assert_not_awaited()

    async def test_falls_back_to_ucs_when_dm_closed(self):
        from src import bot

        with patch.object(bot, "_dm_authorized_user", AsyncMock(return_value=False)) as dm, \
             patch.object(bot, "post_to_text", AsyncMock()) as ucs:
            ok = await bot._notify_user_buzz("buzz <@1>")
            self.assertTrue(ok)
            dm.assert_awaited_once()
            ucs.assert_awaited_once()

    async def test_reports_failure_when_both_paths_fail(self):
        from src import bot

        with patch.object(bot, "_dm_authorized_user", AsyncMock(return_value=False)), \
             patch.object(bot, "post_to_text", AsyncMock(side_effect=RuntimeError("no channel"))):
            ok = await bot._notify_user_buzz("buzz <@1>")
            self.assertFalse(ok)


class TestNarratorOffVoiceDelivery(unittest.IsolatedAsyncioTestCase):
    """Off-voice (`gemini` not connected), ONLY a `question` buzzes; a mere
    finish / error / stall / progress / start stays on the silent audit stream."""

    def _patches(self, *, buzz=True):
        from src import bot

        self._buzz = AsyncMock(return_value=buzz)
        return [
            patch.object(bot, "gemini", None),  # off voice
            patch.object(bot, "conversation", MagicMock()),
            patch.object(bot, "post_to_alerts", AsyncMock()),
            patch.object(bot, "_notify_user_buzz", self._buzz),
        ]

    async def _run(self, evt, *, buzz=True):
        from src import bot

        ctxs = self._patches(buzz=buzz)
        for c in ctxs:
            c.start()
        try:
            await bot._narrate_registry_event(evt)
        finally:
            for c in reversed(ctxs):
                c.stop()

    async def test_question_buzzes(self):
        await self._run(_make_event("question", "high", question="Which approach?"))
        self._buzz.assert_awaited_once()

    async def test_finished_does_not_buzz(self):
        # A window you drive ending is not a question — silent-audit-only.
        await self._run(_make_event("finished", "high"))
        self._buzz.assert_not_awaited()

    async def test_finished_low_severity_does_not_buzz(self):
        await self._run(_make_event("finished", "low"))
        self._buzz.assert_not_awaited()

    async def test_errored_does_not_buzz(self):
        await self._run(_make_event("errored", "high"))
        self._buzz.assert_not_awaited()

    async def test_stalled_does_not_buzz(self):
        await self._run(_make_event("stalled", "low"))
        self._buzz.assert_not_awaited()

    async def test_progress_does_not_buzz(self):
        await self._run(_make_event("progress", "low"))
        self._buzz.assert_not_awaited()

    async def test_started_does_not_buzz(self):
        await self._run(_make_event("started", "low"))
        self._buzz.assert_not_awaited()


class TestNoPhantomAgentFraming(unittest.TestCase):
    """DP1 (forensic 2026-06-19 06:19): an idle IDE window must be framed as a
    window-you-drive, never as an agent waiting for a relayed answer."""

    def _agent(self, source: str):
        from src.cursor_registry import CursorAgent

        a = CursorAgent(
            agent_id="/tmp/x", workspace_root="/tmp/x",
            project_label="x", source=source,
        )
        a.pending_question = "Proceed to Phase 3 or finish verification first?"
        return a

    def test_ide_question_dm_does_not_promise_to_relay(self):
        from src import bot
        from src.cursor_registry import RegistryEvent

        evt = RegistryEvent(kind="question", agent=self._agent("ide"),
                            severity="high", reason="x is asking")
        dm = bot._format_registry_dm(evt)
        self.assertNotIn("relay", dm.lower())
        self.assertIn("window you drive", dm.lower())

    def test_sdk_question_dm_still_relays(self):
        from src import bot
        from src.cursor_registry import RegistryEvent

        evt = RegistryEvent(kind="question", agent=self._agent("sdk"),
                            severity="high", reason="x is asking")
        dm = bot._format_registry_dm(evt)
        self.assertIn("relay", dm.lower())

    def test_ide_inject_context_forbids_fake_delivery(self):
        from src import bot
        from src.cursor_registry import RegistryEvent

        evt = RegistryEvent(kind="question", agent=self._agent("ide"),
                            severity="high", reason="x is asking")
        ctx = bot._format_registry_context_for_inject(evt)
        self.assertIn("no background agent", ctx.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
