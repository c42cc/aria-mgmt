"""Proof of the ground primitive — the root fix for the honeycomb forensic
(2026-06-12): a simple project-scoped question cost $11.70 / 17 Opus
iterations / 26 commands because the agent loop started blind (no project
map, no working set), discarded paid-for findings at its budget wall, and
re-billed an uncached, uncompacted context every step.

What these tests prove, layer by layer:

1. Ground + projects render into every loop's context (`_build_context`),
   so referents resolve in zero discovery iterations.
2. The findings ledger survives a loop's exit and is injected into the
   thread's next run — "keep going" resumes instead of re-buying discovery.
3. Context economics: cache breakpoints are placed correctly, old tool
   results are compacted, the cost cap stops BEFORE overshoot, and cache
   token streams are billed honestly.
4. The discovery backstop converts a blind all-search grind into a cheap
   stop with the one question — and the classifier treats an exploratory
   path miss as information, not a wall.
5. Room continuity: a new thread inherits the same channel's recent
   user/aria exchange (so "that" resolves) without re-introducing the
   cursor-watch bleed.

Run with:
    .venv/bin/python -m unittest tests.test_ground_primitive -v
"""

from __future__ import annotations

import contextlib
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

from tests.test_stuck_loop_governor import (  # noqa: E402
    _Block,
    _FakeClient,
    _FakeMcp,
    _Resp,
    _SHELL_OK,
    _patched_loop,
)


# --------------------------------------------------------------------------
# 1. Durable state: ground and findings tables round-trip through a real
#    (temporary) SQLite file via the same init_db the bot runs at boot.
# --------------------------------------------------------------------------

class TestGroundStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db_patch = patch(
            "src.db.DB_PATH", os.path.join(self._tmp.name, "state.db")
        )
        self._db_patch.start()
        from src import db
        db.init_db()
        self.db = db

    def tearDown(self):
        self._db_patch.stop()
        self._tmp.cleanup()

    def test_ground_roundtrip_and_upsert(self):
        self.db.set_ground("active_plan", "ground primitive plan",
                           detail="thread 123", source="123")
        self.db.set_ground("active_project", "live_visuals_3",
                           path="/tmp/lv3", source="cursor_spawn")
        rows = {r["role"]: r for r in self.db.get_ground()}
        self.assertEqual(rows["active_plan"]["label"], "ground primitive plan")
        self.assertEqual(rows["active_project"]["path"], "/tmp/lv3")

        # Upsert replaces, never duplicates.
        self.db.set_ground("active_plan", "revised plan", source="456")
        rows = self.db.get_ground()
        plans = [r for r in rows if r["role"] == "active_plan"]
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["label"], "revised plan")

    def test_ground_rejects_empty_binding(self):
        with self.assertRaises(ValueError):
            self.db.set_ground("", "label")
        with self.assertRaises(ValueError):
            self.db.set_ground("role", "   ")

    def test_findings_roundtrip_and_replacement(self):
        self.assertIsNone(self.db.get_findings("T1"))
        self.db.save_findings("T1", "- found lv3 at /tmp/lv3", "blocked")
        row = self.db.get_findings("T1")
        self.assertIn("/tmp/lv3", row["findings"])
        self.assertEqual(row["status"], "blocked")

        self.db.save_findings("T1", "- answer delivered", "completed")
        row = self.db.get_findings("T1")
        self.assertEqual(row["status"], "completed")
        self.assertNotIn("/tmp/lv3", row["findings"])

        # Empty input is a no-op, not a noise row.
        self.db.save_findings("T2", "   ", "completed")
        self.assertIsNone(self.db.get_findings("T2"))


# --------------------------------------------------------------------------
# 2. The loop's first message carries the projects map and ground bindings —
#    the agent must never pay to discover a registered path.
# --------------------------------------------------------------------------

class TestContextCarriesGround(unittest.TestCase):
    def test_projects_and_ground_render(self):
        from src import tools
        bindings = [{
            "role": "active_plan",
            "label": "unify capture system",
            "path": None,
            "detail": "planning thread 999",
            "source": "999",
            "updated_at": "2026-06-12T00:00:00+00:00",
        }]
        with patch.dict(tools.PROJECT_REGISTRY,
                        {"lv3": "/nonexistent/lv3", "ucs": str(ROOT)},
                        clear=True), \
             patch.object(tools, "get_ground", lambda: bindings):
            ctx = tools._build_context("S")

        self.assertIn("projects (name → absolute path", ctx)
        self.assertIn(f"ucs → {ROOT}", ctx)
        # A stale registry entry is surfaced loudly, never silently dropped.
        self.assertIn("lv3 → /nonexistent/lv3  [MISSING ON DISK]", ctx)
        self.assertIn("ground (durable working set", ctx)
        self.assertIn("active_plan: unify capture system — planning thread 999", ctx)

    def test_rel_age_renders(self):
        from src.tools import _rel_age
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self.assertEqual(_rel_age(now.isoformat()), "just now")
        self.assertTrue(_rel_age((now - timedelta(minutes=10)).isoformat()).endswith("m ago"))
        self.assertTrue(_rel_age((now - timedelta(hours=5)).isoformat()).endswith("h ago"))
        self.assertTrue(_rel_age((now - timedelta(days=3)).isoformat()).endswith("d ago"))
        self.assertEqual(_rel_age("garbage"), "unknown age")


# --------------------------------------------------------------------------
# 3. Context economics — cache breakpoints, compaction, honest cache billing.
# --------------------------------------------------------------------------

class TestCacheBreakpoints(unittest.TestCase):
    def test_system_and_tools_marked(self):
        from src import tools
        blocks = tools._cache_marked_system("SYSTEM")
        self.assertEqual(blocks[0]["cache_control"], {"type": "ephemeral"})

        catalog = [{"name": "a"}, {"name": "b"}]
        marked = tools._cache_marked_tools(catalog)
        self.assertNotIn("cache_control", marked[0])
        self.assertEqual(marked[-1]["cache_control"], {"type": "ephemeral"})
        # The caller's catalog is not mutated.
        self.assertNotIn("cache_control", catalog[-1])

    def test_moving_message_breakpoint_is_single(self):
        from src import tools
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "task"}]},
            {"role": "assistant", "content": "sdk-objects-opaque"},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "r1"},
            ]},
        ]
        tools._move_message_cache_breakpoint(messages)
        self.assertEqual(
            messages[2]["content"][-1]["cache_control"], {"type": "ephemeral"}
        )
        # Next iteration: marker MOVES (old one stripped) — max-4-breakpoint
        # budget is never exceeded.
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": "r2"},
        ]})
        tools._move_message_cache_breakpoint(messages)
        marked = [
            b for m in messages if isinstance(m.get("content"), list)
            for b in m["content"]
            if isinstance(b, dict) and "cache_control" in b
        ]
        self.assertEqual(len(marked), 1)
        self.assertEqual(messages[3]["content"][-1]["cache_control"],
                         {"type": "ephemeral"})


class TestCompaction(unittest.TestCase):
    def test_old_results_clipped_recent_kept_idempotent(self):
        from src import tools
        big = "x" * 10_000

        def carrier(i):
            return {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}", "content": big},
            ]}

        messages = [
            {"role": "user", "content": [{"type": "text", "text": "task"}]},
            carrier(1), carrier(2), carrier(3),
        ]
        n = tools._compact_old_tool_results(messages)
        self.assertEqual(n, 1)
        self.assertIn("…compacted", messages[1]["content"][0]["content"])
        self.assertEqual(messages[2]["content"][0]["content"], big)
        self.assertEqual(messages[3]["content"][0]["content"], big)
        # The task message is never a compaction target.
        self.assertEqual(messages[0]["content"][0]["text"], "task")
        # Idempotent: a second pass finds nothing left to cut.
        self.assertEqual(tools._compact_old_tool_results(messages), 0)


class TestCacheAwareBilling(unittest.TestCase):
    def test_all_four_token_streams_billed(self):
        from src import tools
        cost_in, cost_out = tools._get_model_costs()
        got = tools._estimate_cost(1000, 1000, 1000, 1000)
        expected = (
            1000 / 1e6 * cost_in
            + 1000 / 1e6 * cost_in * tools._CACHE_WRITE_MULT
            + 1000 / 1e6 * cost_in * tools._CACHE_READ_MULT
            + 1000 / 1e6 * cost_out
        )
        self.assertAlmostEqual(got, expected, places=9)

    def test_usage_context_tokens_counts_cached(self):
        from src import tools

        class U:
            input_tokens = 100
            output_tokens = 5
            cache_creation_input_tokens = 200
            cache_read_input_tokens = 300

        self.assertEqual(tools._usage_context_tokens(U()), 600)


# --------------------------------------------------------------------------
# 4. Discovery: an exploratory path miss is PROGRESS, not a wall — and a
#    blind all-discovery grind stops at the discovery cap with the question.
# --------------------------------------------------------------------------

class TestDiscoveryClassification(unittest.TestCase):
    def test_path_miss_from_discovery_is_progress(self):
        from src.outcomes import classify_outcome
        miss = json.dumps({
            "stdout": "",
            "stderr": "ls: /Users/corbin/live_visuals_3: No such file or directory",
            "exitCode": 1,
        })
        out = classify_outcome("execute_command", {"command": "ls /Users/corbin/live_visuals_3"}, miss)
        self.assertTrue(out.is_progress)

    def test_path_miss_from_non_discovery_is_wall(self):
        from src.outcomes import classify_outcome
        miss = json.dumps({
            "stdout": "",
            "stderr": "cat: /etc/target.conf: No such file or directory",
            "exitCode": 1,
        })
        out = classify_outcome("execute_command", {"command": "cat /etc/target.conf"}, miss)
        self.assertTrue(out.is_blocked)

    def test_auth_wall_still_blocks_for_discovery_tools(self):
        from src.outcomes import classify_outcome
        denied = json.dumps({
            "stdout": "", "stderr": "find: Permission denied", "exitCode": 1,
        })
        out = classify_outcome("execute_command", {"command": "find /root"}, denied)
        self.assertTrue(out.is_blocked)


class TestDiscoveryBackstop(unittest.IsolatedAsyncioTestCase):
    async def test_all_discovery_spend_stops_with_question(self):
        from src import tools
        # The model greps forever with unique args (dedup never trips); every
        # result is clean exit-0 emptiness. Only the discovery governor stops it.
        client = _FakeClient([], tail=lambda n: _Resp([_Block(
            "tool_use", name="execute_command",
            input={"command": f"grep -r 'plan' /Users/x/{n}"}, id=f"t{n}")]))
        mcp = _FakeMcp(_SHELL_OK)
        with _patched_loop(client, mcp, cost_per_call=0.6) as t:
            result = await t._do_with_claude_loop("mark the plan's todos", "K")

        self.assertIn("Blocked", result)
        self.assertIn("purely on discovery", result)
        self.assertIn("set_ground", result)
        # cost 0.6/step, cap $1.50 -> stops after the 3rd step (1.8 >= 1.5),
        # nowhere near the $5 wall or the 30-iteration cap.
        self.assertEqual(client.messages.calls, 3)

    async def test_one_grounded_call_disarms_backstop(self):
        """A loop that touches ANY non-discovery tool is doing work, not
        blind searching — it must run to its normal completion."""
        script = [
            _Resp([_Block("tool_use", name="execute_command",
                          input={"command": "grep -r honeycomb /tmp/lv3"}, id="t1")]),
            _Resp([_Block("tool_use", name="read_text_file",
                          input={"path": "/tmp/lv3/server/telemetry.py"}, id="t2")]),
            _Resp([_Block("tool_use", name="execute_command",
                          input={"command": "grep HONEYCOMB /tmp/lv3/.env"}, id="t3")]),
            _Resp([_Block("text", text="DONE: honeycomb is wired in telemetry.py")],
                  stop_reason="end_turn"),
        ]
        client = _FakeClient(script)
        mcp = _FakeMcp(_SHELL_OK)
        with _patched_loop(client, mcp, cost_per_call=0.6) as t:
            result = await t._do_with_claude_loop("how is honeycomb wired?", "K")

        self.assertIn("DONE: honeycomb is wired", result)
        self.assertNotIn("Blocked", result)


# --------------------------------------------------------------------------
# 5. The findings ledger: saved on every exit, injected into the next run in
#    the same thread — paid-for discovery survives the budget wall.
# --------------------------------------------------------------------------

class TestFindingsLedger(unittest.IsolatedAsyncioTestCase):
    async def test_ledger_saved_on_spend_stop(self):
        from src import tools
        saved = {}
        client = _FakeClient([], tail=lambda n: _Resp([_Block(
            "tool_use", name="search_emails",
            input={"q": f"page-{n}"}, id=f"t{n}")]))
        mcp = _FakeMcp(_SHELL_OK)
        with _patched_loop(client, mcp, cost_per_call=2.0) as t, \
             patch.object(t, "save_findings",
                          lambda sk, text, status: saved.update(
                              {"sk": sk, "text": text, "status": status})):
            await t._do_with_claude_loop("read everything", "K")

        self.assertEqual(saved["sk"], "K")
        self.assertEqual(saved["status"], "blocked")
        self.assertIn("search_emails", saved["text"])

    async def test_prior_findings_injected_into_next_run(self):
        from src import tools
        seen_first_message = {}

        class _RecordingMessages:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    seen_first_message["text"] = kwargs["messages"][0]["content"][0]["text"]
                return _Resp([_Block("text", text="resumed and finished")],
                             stop_reason="end_turn")

        class _RecordingClient:
            def __init__(self):
                self.messages = _RecordingMessages()

        prior = {
            "session_key": "K",
            "findings": "- grep => honeycomb at /tmp/lv3/server/telemetry.py",
            "status": "blocked",
            "updated_at": "2026-06-12T00:00:00+00:00",
        }
        client = _RecordingClient()
        mcp = _FakeMcp(_SHELL_OK)
        with _patched_loop(client, mcp) as t, \
             patch.object(t, "get_findings", lambda sk: prior if sk == "K" else None):
            result = await t._do_with_claude_loop("keep going", "K")

        self.assertIn("resumed and finished", result)
        self.assertIn("do NOT re-run discovery", seen_first_message["text"])
        self.assertIn("/tmp/lv3/server/telemetry.py", seen_first_message["text"])

    def test_distill_findings_is_mechanical_and_bounded(self):
        from src.tools import _distill_findings
        trace = [
            {"tool": "execute_command", "args": {"command": "grep -r x /a"},
             "result": "match: /a/b.py\n" * 200, "deduped": False},
            {"tool": "read_text_file", "args": {"path": "/a/b.py"},
             "result": "contents…", "deduped": True},  # dup hits excluded
        ]
        text = _distill_findings(trace, cap_chars=500)
        self.assertIn("execute_command", text)
        self.assertNotIn("read_text_file", text)
        self.assertLessEqual(len(text), 500)


# --------------------------------------------------------------------------
# 6. Room continuity without bleed: a NEW thread inherits the same channel's
#    user/aria exchange; other channels and the cursor-watch stay out.
# --------------------------------------------------------------------------

class TestRoomContinuity(unittest.TestCase):
    def test_new_thread_inherits_same_channel_exchange(self):
        from src.conversation import ConversationBuffer
        buf = ConversationBuffer()
        buf.add_user_text("#t-old", "ssh-ed25519 SHA256:TI8mK2…fingerprint",
                          session_key="OLD", parent_channel="ucs-chan")
        buf.add_aria_text("#t-old", "I see an SSH key fingerprint — what should I do with it?",
                          session_key="OLD", parent_channel="ucs-chan")
        ctx = buf.as_claude_context(
            max_turns=10, session_key="NEW", parent_channel="ucs-chan"
        )
        self.assertIn("fingerprint", ctx)
        self.assertIn("what should I do with it", ctx)

    def test_other_channel_stays_out(self):
        from src.conversation import ConversationBuffer
        buf = ConversationBuffer()
        buf.add_user_text("#t-keys", "tailscale key rotation",
                          session_key="OLD", parent_channel="keys-chan")
        ctx = buf.as_claude_context(
            max_turns=10, session_key="NEW", parent_channel="ucs-chan"
        )
        self.assertEqual(ctx, "")

    def test_cursor_watch_still_excluded_from_focused_thread(self):
        from src.conversation import ConversationBuffer
        buf = ConversationBuffer()
        buf.add_user_text("#t-old", "earlier message",
                          session_key="OLD", parent_channel="ucs-chan")
        buf.add_cursor_event("[Cursor watch: live_visuals_4 assistant turn]")
        ctx = buf.as_claude_context(
            max_turns=10, session_key="NEW", parent_channel="ucs-chan"
        )
        self.assertIn("earlier message", ctx)
        self.assertNotIn("live_visuals_4", ctx)

    def test_no_parent_channel_keeps_strict_isolation(self):
        from src.conversation import ConversationBuffer
        buf = ConversationBuffer()
        buf.add_user_text("#t-old", "old request", session_key="OLD",
                          parent_channel="ucs-chan")
        ctx = buf.as_claude_context(max_turns=10, session_key="NEW")
        self.assertEqual(ctx, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
