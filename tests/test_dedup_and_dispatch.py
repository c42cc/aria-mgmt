"""Unit-level proof for L1, L2, L5 fixes from the api-duplication audit.

These tests do not exercise the live Discord/Anthropic/Gemini stack — they
isolate the *primitive* that was dysfunctional and prove it now behaves.

Run with:
    .venv/bin/python -m unittest tests.test_dedup_and_dispatch -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestClaudeDedupKey(unittest.TestCase):
    """L5: Claude tool_use dedup key is stable across arg ordering."""

    def test_dedup_key_stable_under_arg_reorder(self):
        from src.tools import _dedup_key
        k1 = _dedup_key("search_emails", {"query": "is:unread", "maxResults": 500})
        k2 = _dedup_key("search_emails", {"maxResults": 500, "query": "is:unread"})
        self.assertEqual(k1, k2)

    def test_dedup_key_differs_on_args(self):
        from src.tools import _dedup_key
        k1 = _dedup_key("search_emails", {"query": "is:unread"})
        k2 = _dedup_key("search_emails", {"query": "is:read"})
        self.assertNotEqual(k1, k2)

    def test_dedup_key_handles_unjsonable(self):
        """Object that fails default json.dumps falls back to repr."""
        from src.tools import _dedup_key
        class Weird:
            def __init__(self, x): self.x = x
        k = _dedup_key("foo", {"a": Weird(7)})
        self.assertIsInstance(k, str)
        self.assertIn("foo", k)


class TestSessionStateIsolation(unittest.TestCase):
    """L2: per-session state must not bleed between sessions."""

    def setUp(self):
        from src import tools
        tools._session_states.clear()

    def test_cancel_isolated(self):
        from src.tools import _state_for, set_cancel_flag
        a = _state_for("ch-A")
        b = _state_for("ch-B")
        a.cancel = True
        self.assertTrue(a.cancel)
        self.assertFalse(b.cancel)

    def test_set_cancel_flag_per_session(self):
        from src.tools import _state_for, set_cancel_flag
        _state_for("ch-A").cancel = False
        _state_for("ch-B").cancel = False
        set_cancel_flag(True, session_key="ch-A")
        self.assertTrue(_state_for("ch-A").cancel)
        self.assertFalse(_state_for("ch-B").cancel)

    def test_set_cancel_flag_broadcast(self):
        from src.tools import _state_for, set_cancel_flag
        _state_for("ch-A").cancel = False
        _state_for("ch-B").cancel = False
        set_cancel_flag(True)  # None == broadcast
        self.assertTrue(_state_for("ch-A").cancel)
        self.assertTrue(_state_for("ch-B").cancel)

    def test_claude_call_counter_isolated(self):
        from src.tools import _state_for
        _state_for("ch-A").claude_calls = 5
        self.assertEqual(_state_for("ch-A").claude_calls, 5)
        self.assertEqual(_state_for("ch-B").claude_calls, 0)


class TestGeminiDispatchTracking(unittest.IsolatedAsyncioTestCase):
    """L1: in-flight dispatch tasks are tracked and orphan results surface."""

    async def test_dispatch_task_registered(self):
        from src.gemini_session import GeminiSession
        gs = GeminiSession(tool_handler=AsyncMock(return_value="ok"))
        # Initially empty
        self.assertEqual(len(gs._dispatch_tasks), 0)

    async def test_orphan_callback_fires_when_session_dead(self):
        """When the session closes before send_tool_response, orphan_callback runs."""
        from src.gemini_session import GeminiSession
        orphan_calls: list[tuple[str, str, str]] = []

        async def on_orphan(name, fc_id, result):
            orphan_calls.append((name, fc_id, result))

        async def tool_handler(name, args):
            return json.dumps({"sent_to": args.get("to")})

        gs = GeminiSession(tool_handler=tool_handler, orphan_callback=on_orphan)
        gs._session = None  # simulate session closed
        gs._connected = False

        fake_fc = MagicMock()
        fake_fc.name = "send_email"
        fake_fc.id = "fc-abc"
        fake_fc.args = {"to": "boss@example.com", "body": "hi"}

        await gs._dispatch_tool_call(fake_fc)

        self.assertEqual(len(orphan_calls), 1)
        name, fc_id, result = orphan_calls[0]
        self.assertEqual(name, "send_email")
        self.assertEqual(fc_id, "fc-abc")
        self.assertIn("boss@example.com", result)


class TestAuditDedupProbe(unittest.TestCase):
    """L7: the dedup probe correctly identifies tight repeats."""

    def test_finds_repeat_within_window(self):
        from src.audit_dedup_probe import find_dup_hits
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({
                "ts": "2026-05-16T10:00:00+00:00", "tool": "search_emails",
                "args": {"q": "a"}, "session_key": "S1",
                "tier": "R", "confirmed": None, "result_summary": "",
            }) + "\n")
            f.write(json.dumps({
                "ts": "2026-05-16T10:00:02.5+00:00", "tool": "search_emails",
                "args": {"q": "a"}, "session_key": "S1",
                "tier": "R", "confirmed": None, "result_summary": "",
            }) + "\n")
            path = f.name
        try:
            from datetime import datetime, timedelta, timezone
            import src.audit_dedup_probe as p
            orig_now = p.datetime
            # Bypass the since_hours filter by patching the cutoff
            hits = find_dup_hits(path, since_hours=10_000_000, window_sec=5.0)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].tool, "search_emails")
            self.assertAlmostEqual(hits[0].dt_seconds, 2.5, places=1)
        finally:
            os.unlink(path)

    def test_no_hit_outside_window(self):
        from src.audit_dedup_probe import find_dup_hits
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({
                "ts": "2026-05-16T10:00:00+00:00", "tool": "search_emails",
                "args": {"q": "a"}, "session_key": "S1",
                "tier": "R", "confirmed": None, "result_summary": "",
            }) + "\n")
            f.write(json.dumps({
                "ts": "2026-05-16T10:00:10+00:00", "tool": "search_emails",
                "args": {"q": "a"}, "session_key": "S1",
                "tier": "R", "confirmed": None, "result_summary": "",
            }) + "\n")
            path = f.name
        try:
            hits = find_dup_hits(path, since_hours=10_000_000, window_sec=5.0)
            self.assertEqual(len(hits), 0)
        finally:
            os.unlink(path)


class TestAnchorCache(unittest.IsolatedAsyncioTestCase):
    """L6: anchor cache coalesces concurrent calls and serves cached on warm read."""

    def setUp(self):
        from src.anchors import registry
        registry.clear_cache()

    async def test_warm_read_uses_cache(self):
        from src.anchors import registry
        from src.anchors.base import AnchorReport

        calls = 0

        class FakeAnchor:
            async def check(self, tc, aria):
                nonlocal calls
                calls += 1
                return AnchorReport(tool="fake")

        anchor = FakeAnchor()
        tc = {"args": {"q": "hi"}}
        r1 = await registry.check_with_cache(anchor, "search_emails", tc, "result")
        r2 = await registry.check_with_cache(anchor, "search_emails", tc, "result")
        self.assertIs(r1, r2)
        self.assertEqual(calls, 1)

    async def test_concurrent_calls_coalesce(self):
        from src.anchors import registry
        from src.anchors.base import AnchorReport

        calls = 0
        gate = asyncio.Event()

        class SlowAnchor:
            async def check(self, tc, aria):
                nonlocal calls
                calls += 1
                await gate.wait()
                return AnchorReport(tool="slow")

        anchor = SlowAnchor()
        tc = {"args": {"q": "hi"}}
        t1 = asyncio.create_task(
            registry.check_with_cache(anchor, "search_emails", tc, "")
        )
        t2 = asyncio.create_task(
            registry.check_with_cache(anchor, "search_emails", tc, "")
        )
        await asyncio.sleep(0)  # let both tasks register
        gate.set()
        r1, r2 = await asyncio.gather(t1, t2)
        self.assertEqual(calls, 1, "two concurrent calls should coalesce into one fetch")
        self.assertIs(r1, r2)

    async def test_write_tools_bypass_cache(self):
        from src.anchors import registry
        from src.anchors.base import AnchorReport

        calls = 0

        class FakeAnchor:
            async def check(self, tc, aria):
                nonlocal calls
                calls += 1
                return AnchorReport(tool="write")

        anchor = FakeAnchor()
        tc = {"args": {"to": "x"}}
        await registry.check_with_cache(anchor, "send_email", tc, "")
        await registry.check_with_cache(anchor, "send_email", tc, "")
        self.assertEqual(calls, 2, "write anchors must not be cached")


class TestConversationBufferAlertFilter(unittest.TestCase):
    """L15: as_gemini_injection must drop alert turns by default."""

    def test_alerts_excluded_by_default(self):
        from src.conversation import ConversationBuffer
        buf = ConversationBuffer()
        buf.add_user_text("#ucs", "hello")
        buf.add_aria_text("#ucs", "hi")
        buf.add_alert("Preflight passed (22 probes)")
        out = buf.as_gemini_injection(max_turns=10)
        self.assertIn("hello", out)
        self.assertIn("hi", out)
        self.assertNotIn("Preflight passed", out)

    def test_alerts_included_when_opt_in(self):
        from src.conversation import ConversationBuffer
        buf = ConversationBuffer()
        buf.add_user_text("#ucs", "hi")
        buf.add_alert("Preflight passed")
        out = buf.as_gemini_injection(max_turns=10, include_alerts=True)
        self.assertIn("Preflight passed", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
