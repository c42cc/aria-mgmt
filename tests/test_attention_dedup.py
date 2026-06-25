"""Attention backpressure (forensic 2026-06-25): duplicate STOP buzzes from one
thread (conversation_log 694/698/710 "completed", 701/704/706 "cancelled") must
not double-ping the phone. A same-kind repeat within the cooldown is folded into
the silent audit; a distinct kind, a post-cooldown stop, and a different thread
all still buzz. Nothing is dropped — the locked "every distinct stop surfaces"
policy holds.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _agent(agent_id: str = "/tmp/aria_dedup"):
    from src.cursor_registry import CursorAgent
    a = CursorAgent(
        agent_id=agent_id, workspace_root=agent_id,
        project_label="aria_dedup", source="ide",
    )
    a.last_assistant_text = "Did the thing."
    a.last_event_at = 1.0
    return a


def _evt(agent, kind):
    from src.cursor_registry import RegistryEvent
    return RegistryEvent(kind=kind, agent=agent, severity="high",
                         reason=f"Cursor {kind} in aria_dedup.")


class AttentionDedup(unittest.IsolatedAsyncioTestCase):
    async def _narrate(self, evt, buzz):
        from src import bot
        ctxs = [
            patch.object(bot, "gemini", None),  # off voice
            patch.object(bot, "conversation", MagicMock()),
            patch.object(bot, "post_to_alerts", AsyncMock()),
            patch.object(bot, "_notify_user_buzz", buzz),
        ]
        for c in ctxs:
            c.start()
        try:
            await bot._narrate_registry_event(evt)
        finally:
            for c in reversed(ctxs):
                c.stop()

    async def test_duplicate_same_kind_folds(self):
        buzz = AsyncMock(return_value=True)
        agent = _agent()
        await self._narrate(_evt(agent, "finished"), buzz)
        await self._narrate(_evt(agent, "finished"), buzz)  # within cooldown -> folded
        self.assertEqual(buzz.await_count, 1)

    async def test_distinct_kind_still_buzzes(self):
        buzz = AsyncMock(return_value=True)
        agent = _agent()
        await self._narrate(_evt(agent, "finished"), buzz)
        await self._narrate(_evt(agent, "errored"), buzz)
        self.assertEqual(buzz.await_count, 2)

    async def test_post_cooldown_rebuzzes(self):
        from src import bot
        buzz = AsyncMock(return_value=True)
        agent = _agent()
        await self._narrate(_evt(agent, "finished"), buzz)
        agent.last_buzzed_at -= bot._BUZZ_DEDUP_COOLDOWN_S + 1  # simulate cooldown expiry
        await self._narrate(_evt(agent, "finished"), buzz)
        self.assertEqual(buzz.await_count, 2)

    async def test_distinct_threads_are_independent(self):
        buzz = AsyncMock(return_value=True)
        await self._narrate(_evt(_agent("/tmp/a"), "finished"), buzz)
        await self._narrate(_evt(_agent("/tmp/b"), "finished"), buzz)
        self.assertEqual(buzz.await_count, 2)


if __name__ == "__main__":
    unittest.main()
