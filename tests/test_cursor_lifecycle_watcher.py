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


def _machinery_followup() -> dict:
    """Cursor's injected post-task follow-up — a user-ROLE turn that is not the
    human (verbatim shape from the 2026-07-02 duplicate-buzz forensic)."""
    return _user(
        "<timestamp>Thursday, Jul 2, 2026, 1:15 AM (UTC-7)</timestamp>\n\n"
        "<user_query>Briefly inform the user about the task result and perform "
        "any follow-up actions (if needed).</user_query>"
    )


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

    async def _attach(self, sid: str, rows: list[dict] | None = None, old: bool = False):
        """Attach a tailer and let it SEED first. Content present at attach is
        HISTORY only if the transcript is OLD (quiet past _ATTACH_HISTORY_SECONDS);
        a freshly-written transcript is a live/just-handed-back thread the watcher
        must surface. `old=True` ages the file so it counts as history. Returns the
        path; bytes appended after this returns are always live activity."""
        from src.cursor_registry import SessionInfo
        p = os.path.join(self.tmp, f"{sid}.jsonl")
        with open(p, "w") as f:
            for r in (rows or []):
                f.write(json.dumps(r) + "\n")
        if old:
            past = time.time() - 600  # 10 min ago -> history
            os.utime(p, (past, past))
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

    async def test_fresh_handback_at_attach_emits(self):
        # THE bug: a thread whose FIRST hook attaches us at its own turn-end. The
        # transcript already holds the hand-back, but it is FRESH -> it must still
        # ping (the old seed-to-filesize swallowed exactly this, so aria/this very
        # chat never pinged). Attach to a just-handed-back transcript, append
        # nothing, and require one finished.
        await self._attach("sidfresh0", rows=[_user("audit it"), _atext("Done — here is the summary.")])
        await asyncio.sleep(0.45)
        fin = self._kinds("finished")
        self.assertEqual(len(fin), 1, "a fresh hand-back present at attach must ping exactly once")
        self.assertIn("sidfresh", fin[0].reason)

    async def test_old_finished_thread_is_suppressed_on_attach(self):
        # A thread that finished LONG before we attached (restart / discovery of an
        # old thread) is history: seeded past, never replayed as a stale burst.
        await self._attach(
            "sidold00", rows=[_user("do it"), _atext("finished 10 minutes ago")], old=True
        )
        await asyncio.sleep(0.45)
        self.assertEqual(self._kinds("finished"), [], "an old settled halt is history, never replayed")

    async def test_machinery_followup_never_rebuzzes_the_same_handback(self):
        # THE 2026-07-02 duplicate: finish -> Cursor injects "Briefly inform the
        # user…" -> the agent's one-line coda settles -> a second "finished" for
        # the SAME hand-back buzzed 24s after the first. Machinery growth must
        # advance the watermark silently; only a HUMAN turn re-arms.
        p = await self._attach("sidmach00")
        _append(p, [_atext("real work done — handing back")])
        await asyncio.sleep(0.4)
        self.assertEqual(len(self._kinds("finished")), 1)

        _append(p, [_machinery_followup(), _machinery_followup(),
                    _atext("Both of those were already folded into the work.")])
        await asyncio.sleep(0.4)
        self.assertEqual(
            len(self._kinds("finished")), 1,
            "a machinery follow-up coda re-buzzed the same hand-back",
        )

        # A real human turn re-arms: the next hand-back is genuinely new.
        _append(p, [_user("now tighten the tests"), _atext("tightened — done")])
        await asyncio.sleep(0.4)
        self.assertEqual(len(self._kinds("finished")), 2)

    async def test_repeat_stall_without_human_input_is_one_buzz(self):
        # A hang that keeps dribbling tool bytes and hanging again is the SAME
        # stall — one buzz until a human pokes it or the state changes kind.
        p = await self._attach("sidstall0")
        _append(p, [_atool("Shell")])
        await asyncio.sleep(0.9)  # settle + hung (0.6s in test config)
        self.assertEqual(len(self._kinds("stalled")), 1)

        _append(p, [_atool("Shell")])  # more machinery bytes, still no hand-back
        await asyncio.sleep(0.9)
        self.assertEqual(len(self._kinds("stalled")), 1, "the same hang re-nagged")

        # By now the fully-delivered quiet tailer has reaped; production
        # re-attaches on resume via the discovery sweep — mimic that.
        _append(p, [_atext("finally finished — handing back")])
        await self.reg.ensure_thread_from_disk("/proj/live_visuals_4", "sidstall0", p)
        await asyncio.sleep(0.4)
        self.assertEqual(len(self._kinds("finished")), 1, "a kind CHANGE still surfaces")

    async def test_machinery_turn_does_not_answer_a_pending_question(self):
        # An AskQuestion halt buzzes; Cursor's injected follow-up must not count
        # as "the user answered" and erase the pending decision.
        p = await self._attach("sidqmach0")
        _append(p, [{
            "role": "assistant",
            "message": {"content": [
                {"type": "text", "text": "I need a decision."},
                {"type": "tool_use", "name": "AskQuestion",
                 "input": {"questions": [{"prompt": "Ship v1 or wait?"}]}},
            ]},
        }])
        await asyncio.sleep(0.45)
        self.assertEqual(len(self._kinds("question")), 1)
        _append(p, [_machinery_followup()])
        await asyncio.sleep(0.3)
        agent = self.reg._get_or_create("/proj/live_visuals_4", source="ide")
        self.assertIsNotNone(
            agent.sessions["sidqmach0"].pending_question,
            "machinery erased a pending human decision",
        )

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
