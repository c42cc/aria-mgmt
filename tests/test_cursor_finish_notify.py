"""Contract: the cursor-watch off-voice delivery, after the deterministic collapse.

The IDE phone buzz is now owned by the standalone Cursor `stop` hook
(`hooks/notify-finish.py` -> `src/notify_phone.py`), NOT the tailer-driven
narrator. So:

  * `_notify_user_buzz` routes through the ONE delivery home (`notify_phone`).
    "Delivered" means Discord accepted the DM; a failure is LOUD and returns
    False — never the old silent `#ucs` fallback that logged a fabricated
    "delivered" while DMs were dead all day.
  * The narrator does NOT phone-buzz for `source == "ide"` events (the hook
    owns them) — it keeps the silent #ucs audit and the spoken voice heads-up.
    It STILL phone-buzzes SDK / Claude-Code finishes (no IDE hook fires there).
  * Routine progress/started never buzz. The DM summary is built from REAL
    output, never an invented next step.

Run with:
    .venv/bin/python -m pytest tests/test_cursor_finish_notify.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_event(kind: str, severity: str = "low", *, source: str = "ide",
                reason: str = "", question: str | None = None):
    from src.cursor_registry import CursorAgent, RegistryEvent

    agent = CursorAgent(
        agent_id="/tmp/aria_notify_selftest",
        workspace_root="/tmp/aria_notify_selftest",
        project_label="aria_notify_selftest",
        source=source,
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


class TestNotifyUserBuzzOneHome(unittest.IsolatedAsyncioTestCase):
    """`_notify_user_buzz` routes through the one verified delivery home and is
    honest: True only when Discord accepted the DM, never on a silent fallback."""

    async def test_delegates_to_notify_phone_and_reports_delivered(self):
        from src import bot, notify_phone

        with patch.object(
            notify_phone, "deliver",
            MagicMock(return_value=notify_phone.Result(True, msg_id="42")),
        ) as deliver:
            ok = await bot._notify_user_buzz("buzz <@1>", kind="finished",
                                             project="p", sid="s")
            self.assertTrue(ok)
            deliver.assert_called_once()

    async def test_failed_send_is_loud_never_a_fabricated_success(self):
        from src import bot, notify_phone

        # notify_phone has already alarmed internally; the buzz must report False,
        # never the old "delivered via #ucs" lie.
        with patch.object(
            notify_phone, "deliver",
            MagicMock(return_value=notify_phone.Result(False, error="DMs disabled")),
        ):
            ok = await bot._notify_user_buzz("buzz <@1>")
            self.assertFalse(ok)

    async def test_no_silent_ucs_fallback_path_remains(self):
        # The old fallback called post_to_text on DM failure and logged
        # "delivered". That path must be gone: a failed deliver never touches it.
        from src import bot, notify_phone

        with patch.object(
            notify_phone, "deliver",
            MagicMock(return_value=notify_phone.Result(False, error="x")),
        ), patch.object(bot, "post_to_text", AsyncMock()) as ucs:
            await bot._notify_user_buzz("buzz <@1>")
            ucs.assert_not_awaited()


class TestNarratorIdeSuppressedSdkBuzzes(unittest.IsolatedAsyncioTestCase):
    """Off-voice: IDE stops do NOT phone-buzz (the hook owns them) but still
    audit; SDK/Claude-Code stops DO phone-buzz; progress/started never buzz."""

    def _patches(self):
        from src import bot

        self._buzz = AsyncMock(return_value=True)
        self._alerts = AsyncMock()
        return [
            patch.object(bot, "gemini", None),  # off voice
            patch.object(bot, "conversation", MagicMock()),
            patch.object(bot, "post_to_alerts", self._alerts),
            patch.object(bot, "_notify_user_buzz", self._buzz),
        ]

    async def _run(self, evt):
        from src import bot

        ctxs = self._patches()
        for c in ctxs:
            c.start()
        try:
            await bot._narrate_registry_event(evt)
        finally:
            for c in reversed(ctxs):
                c.stop()

    async def test_ide_finished_does_not_phone_buzz_but_audits(self):
        await self._run(_make_event("finished", "high", source="ide"))
        self._buzz.assert_not_awaited()
        self._alerts.assert_awaited()  # the glanceable #ucs trail stays

    async def test_ide_question_does_not_phone_buzz(self):
        await self._run(_make_event("question", "high", source="ide", question="Which?"))
        self._buzz.assert_not_awaited()

    async def test_sdk_finished_phone_buzzes(self):
        await self._run(_make_event("finished", "high", source="sdk"))
        self._buzz.assert_awaited_once()

    async def test_claude_code_errored_phone_buzzes(self):
        await self._run(_make_event("errored", "high", source="claude_code"))
        self._buzz.assert_awaited_once()

    async def test_progress_never_buzzes(self):
        await self._run(_make_event("progress", "low", source="sdk"))
        self._buzz.assert_not_awaited()

    async def test_started_never_buzzes(self):
        await self._run(_make_event("started", "low", source="sdk"))
        self._buzz.assert_not_awaited()


class TestDmCarriesSummary(unittest.TestCase):
    """Every stop DM carries a factual little summary of what the thread did —
    its own last words / the question / the error — never an invented next step."""

    def test_finished_dm_includes_last_assistant_text(self):
        from src import bot

        evt = _make_event("finished", "high", source="sdk",
                          reason="Cursor task completed in proj.")
        evt.agent.last_assistant_text = "Refactored the auth module and all 40 tests pass."
        dm = bot._format_registry_dm(evt)
        self.assertIn("Cursor task completed", dm)
        self.assertIn("Refactored the auth module", dm)

    def test_question_dm_includes_the_question(self):
        from src import bot

        evt = _make_event("question", "high", source="sdk",
                          reason="proj is asking", question="Ship v1 or wait?")
        dm = bot._format_registry_dm(evt)
        self.assertIn("Ship v1 or wait?", dm)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
