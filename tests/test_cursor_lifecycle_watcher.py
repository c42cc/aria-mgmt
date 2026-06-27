"""The lifecycle watcher, end to end against real transcript files.

This is the runtime definition of the fix: a thread's surfaced state is a pure
function of its TRANSCRIPT, evaluated when it SETTLES — one terminal buzz per
hand-back, never a per-turn hook. These drive the real async tailer against temp
JSONL files (config patched to a fast settle, a temp state.db for the watermark):

- finished fires ONCE at settle, never per turn, and never mid-stream;
- a new hand-back re-arms (a second buzz);
- concurrent threads each buzz their own (no project-level aliasing);
- a restart does NOT replay an already-settled halt (durable watermark seed);
- a thread whose first hook never arrived is still surfaced via disk discovery.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _user(text: str) -> dict:
    return {"role": "user", "message": {"content": [{"type": "text", "text": text}]}}


def _atext(text: str) -> dict:
    return {"role": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _atool(name: str = "Shell") -> dict:
    return {"role": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": {}}]}}


def _append(path: str, rows: list[dict]) -> None:
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class LifecycleWatcher(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_patch = patch("src.db.DB_PATH", os.path.join(self.tmp, "state.db"))
        self.db_patch.start()
        from src.db import init_db
        init_db()
        # Fast, deterministic thresholds (production default is 12s).
        fake = SimpleNamespace(
            data_dir=self.tmp,
            cursor_settle_seconds=0.2,
            cursor_hung_minutes=0.01,  # ~0.6s
            cursor_discovery_seconds=0.2,
        )
        self.cfg_patch = patch("src.config.config", fake)
        self.cfg_patch.start()
        from src import cursor_registry
        self.reg = cursor_registry.CursorAgentRegistry(tail_interval_sec=0.05)
        self.events: list = []

        async def _emit(evt):
            self.events.append(evt)

        self.reg.set_emit_callback(_emit)

    async def asyncTearDown(self):
        await self.reg.stop()
        self.cfg_patch.stop()
        self.db_patch.stop()

    def _kinds(self, kind: str):
        return [e for e in self.events if e.kind == kind]

    async def _attach(self, sid: str, rows: list[dict] | None = None):
        """Attach a tailer and let it SEED first — everything present at attach is
        history (the anti-replay guard). Returns the path; bytes appended after
        this returns are the live activity that can buzz."""
        from src.cursor_registry import SessionInfo
        p = os.path.join(self.tmp, f"{sid}.jsonl")
        with open(p, "w") as f:
            for r in (rows or []):
                f.write(json.dumps(r) + "\n")
        agent = self.reg._get_or_create("/proj/live_visuals_4", source="ide")
        sess = SessionInfo(sid=sid, started_at=time.time(), last_event_at=time.time(), transcript_path=p)
        agent.sessions[sid] = sess
        agent.current_sid = sid
        self.reg._ensure_tailer(agent, sess)
        await asyncio.sleep(0.12)  # let the tailer run once and seed the watermark
        return p

    # NOTE: every call site below awaits _attach (it is async).

    async def test_finished_once_at_settle_never_per_turn(self):
        # Pre-existing user turn = history (seeded, never buzzed).
        p = await self._attach("sidalpha", rows=[_user("do the thing")])
        await asyncio.sleep(0.15)
        # The agent works across several turns, then hands back.
        _append(p, [_atool("Read")])
        await asyncio.sleep(0.08)
        _append(p, [_atext("step one done")])
        await asyncio.sleep(0.08)
        _append(p, [_atext("all done — handed back to you")])
        # Still within the settle window across the appends: NO buzz yet.
        self.assertEqual(self._kinds("finished"), [], "must not buzz per-turn while active")
        # Now quiet past settle -> exactly one finished, thread-identifiable.
        await asyncio.sleep(0.45)
        fin = self._kinds("finished")
        self.assertEqual(len(fin), 1, f"exactly one hand-back buzz, got {len(fin)}")
        self.assertIn("sidalph", fin[0].reason)

    async def test_new_handback_rearms(self):
        p = await self._attach("sidbeta")
        _append(p, [_atext("first answer")])
        await asyncio.sleep(0.4)
        self.assertEqual(len(self._kinds("finished")), 1)
        # User replies, agent answers again, hands back again -> a second buzz.
        _append(p, [_user("now do part two"), _atext("part two done")])
        await asyncio.sleep(0.4)
        self.assertEqual(len(self._kinds("finished")), 2, "a genuinely new hand-back re-buzzes")

    async def test_question_buzzes_finished_does_not_double(self):
        p = await self._attach("sidgamma")
        # Agent stops on an explicit AskQuestion.
        _append(p, [{
            "role": "assistant",
            "message": {"content": [
                {"type": "text", "text": "I need a decision."},
                {"type": "tool_use", "name": "AskQuestion",
                 "input": {"questions": [{"prompt": "Use Redis or in-memory?"}]}},
            ]},
        }])
        await asyncio.sleep(0.45)
        self.assertEqual(len(self._kinds("question")), 1, "an AskQuestion halt buzzes once as a question")
        self.assertEqual(self._kinds("finished"), [], "a question halt is not also a finished")

    async def test_concurrent_threads_each_buzz_their_own(self):
        p1 = await self._attach("sidone11")
        p2 = await self._attach("sidtwo22")
        _append(p1, [_atext("thread one finished")])
        _append(p2, [_atext("thread two finished")])
        await asyncio.sleep(0.5)
        fin = self._kinds("finished")
        self.assertEqual(len(fin), 2)
        reasons = " ".join(e.reason for e in fin)
        self.assertIn("sidone1", reasons)
        self.assertIn("sidtwo2", reasons)

    async def test_restart_does_not_replay_a_settled_halt(self):
        # A thread that already finished BEFORE we attached is history: the
        # watermark is seeded to the current size, so no buzz on (re)attach.
        await self._attach("sidrest0", rows=[_user("do it"), _atext("already finished before watch")])
        await asyncio.sleep(0.45)
        self.assertEqual(self._kinds("finished"), [], "a pre-existing halt is seeded, never replayed")

    async def test_discovery_attaches_a_threadless_of_hooks(self):
        # A thread whose first hook never arrived: discovery's ensure-from-disk
        # attaches a tailer, and a subsequent hand-back surfaces normally.
        p = os.path.join(self.tmp, "siddisc0.jsonl")
        open(p, "w").close()
        await self.reg.ensure_thread_from_disk("/proj/live_visuals_4", "siddisc0", p)
        await asyncio.sleep(0.12)  # let the discovered tailer seed before live bytes
        _append(p, [_atext("discovered + finished")])
        await asyncio.sleep(0.45)
        fin = self._kinds("finished")
        self.assertEqual(len(fin), 1)
        self.assertIn("siddisc", fin[0].reason)


if __name__ == "__main__":
    unittest.main()
