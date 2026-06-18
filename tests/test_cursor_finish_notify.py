"""Regression: a finished/errored/asking Cursor thread must ALWAYS reach the
user off-voice.

The bug: off-voice, `_narrate_registry_event` only buzzed when a completion
could be turned into a `propose_next` suggestion. When the model returned
nothing (or the event was a low-severity finish / an error / a question), the
code fell through to a FORCED-SILENT `#ucs-alerts` post and the user got no
notification at all — "a thread finished and Aria never messaged me."

These tests isolate the narrator's off-voice delivery contract and prove every
terminal/actionable transition now lands a guaranteed buzz, while pure
progress/started events stay on the silent stream.

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
    """Off-voice (`gemini` not connected), every terminal/actionable event
    buzzes; progress/started do not."""

    def _patches(self, *, proposal=False, buzz=True):
        from src import bot

        self._buzz = AsyncMock(return_value=buzz)
        self._propose = AsyncMock(return_value=proposal)
        return [
            patch.object(bot, "gemini", None),  # off voice
            patch.object(bot, "conversation", MagicMock()),
            patch.object(bot, "post_to_alerts", AsyncMock()),
            patch.object(bot, "_maybe_propose_next_after_completion", self._propose),
            patch.object(bot, "_notify_user_buzz", self._buzz),
        ]

    async def _run(self, evt, *, proposal=False, buzz=True):
        from src import bot

        ctxs = self._patches(proposal=proposal, buzz=buzz)
        for c in ctxs:
            c.start()
        try:
            await bot._narrate_registry_event(evt)
        finally:
            for c in reversed(ctxs):
                c.stop()

    async def test_finished_low_severity_buzzes_when_no_proposal(self):
        # The 8:50 case: status-less finish + model had no suggestion.
        await self._run(_make_event("finished", "low"), proposal=False)
        self._buzz.assert_awaited_once()

    async def test_finished_high_severity_buzzes_when_proposal_empty(self):
        await self._run(_make_event("finished", "high"), proposal=False)
        self._buzz.assert_awaited_once()

    async def test_finished_proposal_fires_no_direct_buzz(self):
        # When the richer proposal card fires it buzzes on its own; don't double.
        await self._run(_make_event("finished", "high"), proposal=True)
        self._propose.assert_awaited_once()
        self._buzz.assert_not_awaited()

    async def test_errored_buzzes_and_skips_proposal(self):
        await self._run(_make_event("errored", "high"))
        self._propose.assert_not_awaited()  # errors don't go through propose_next
        self._buzz.assert_awaited_once()

    async def test_question_buzzes(self):
        await self._run(_make_event("question", "high", question="Which approach?"))
        self._buzz.assert_awaited_once()

    async def test_progress_does_not_buzz(self):
        await self._run(_make_event("progress", "low"))
        self._buzz.assert_not_awaited()

    async def test_started_does_not_buzz(self):
        await self._run(_make_event("started", "low"))
        self._buzz.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
