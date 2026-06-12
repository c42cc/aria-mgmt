"""Proof that the thread-per-request primitive behaves.

The dysfunctional primitive: a request used to be identified by its Discord
*channel*, so every #ucs message shared one agent lock and one context
window. Two requests collided ("an agent loop is already running for this
session" — judged failure 144) and bled context into each other.

The fix re-keys a request to its own *thread*: session_key == thread id.
These tests isolate that primitive and prove it now holds. They do NOT
exercise the live Discord/Anthropic/Gemini stack — fakes stand in for the
Discord objects (MagicMock(spec=...) so isinstance still resolves).

Run with:
    .venv/bin/python -m unittest tests.test_thread_per_request -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _AsyncCM:
    """Minimal async context manager standing in for channel.typing()."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_thread(thread_id: int, name: str = "t"):
    th = MagicMock(spec=discord.Thread)
    th.id = thread_id
    th.name = name
    th.send = AsyncMock()
    th.typing = MagicMock(return_value=_AsyncCM())
    return th


# ---------------------------------------------------------------------------
# 1. The collision is gone: _do_with_claude serializes per session_key and
#    never returns the "already running" rejection.
# ---------------------------------------------------------------------------

class TestNoCollision(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from src import tools
        tools._session_states.clear()

    async def test_same_key_serializes_both_complete(self):
        """Two requests on the SAME session_key (a follow-up in one thread)
        serialize behind one lock — both complete, neither is rejected."""
        from src import tools

        order: list[str] = []

        async def fake_loop(task, session_key=""):
            order.append(f"start:{task}")
            await asyncio.sleep(0.05)
            order.append(f"end:{task}")
            return f"done:{task}"

        with patch.object(tools, "_do_with_claude_loop", side_effect=fake_loop):
            r1, r2 = await asyncio.gather(
                tools._do_with_claude("A", "thread-1"),
                tools._do_with_claude("B", "thread-1"),
            )

        # Neither result is the old rejection envelope.
        for r in (r1, r2):
            self.assertNotIn("already running", r)
        self.assertEqual({r1, r2}, {"done:A", "done:B"})
        # Serialized: the first fully finished before the second started.
        self.assertEqual(order[0].split(":")[0], "start")
        self.assertEqual(order[1].split(":")[0], "end")
        self.assertEqual(order[2].split(":")[0], "start")

    async def test_different_keys_run_concurrently(self):
        """Two top-level requests get different threads -> different locks ->
        they overlap. This is the parallelism the channel-keyed design killed."""
        from src import tools

        running = 0
        max_concurrent = 0

        async def fake_loop(task, session_key=""):
            nonlocal running, max_concurrent
            running += 1
            max_concurrent = max(max_concurrent, running)
            await asyncio.sleep(0.05)
            running -= 1
            return f"done:{task}"

        with patch.object(tools, "_do_with_claude_loop", side_effect=fake_loop):
            await asyncio.gather(
                tools._do_with_claude("A", "thread-A"),
                tools._do_with_claude("B", "thread-B"),
            )

        self.assertEqual(max_concurrent, 2, "distinct threads must run in parallel")


# ---------------------------------------------------------------------------
# 2. Context isolation: as_claude_context scoped to a session_key shows only
#    that thread's turns (no cross-request bleed), and is unchanged when no
#    session_key is passed (voice path / legacy callers).
# ---------------------------------------------------------------------------

class TestContextIsolation(unittest.TestCase):
    def test_session_scoped_context_excludes_other_threads(self):
        from src.conversation import ConversationBuffer
        buf = ConversationBuffer()
        buf.add_user_text("#t-A", "request A about emails", session_key="A")
        buf.add_aria_text("#t-A", "answer A", session_key="A")
        buf.add_user_text("#t-B", "request B about calendar", session_key="B")

        ctx_a = buf.as_claude_context(max_turns=10, session_key="A")
        self.assertIn("request A about emails", ctx_a)
        self.assertNotIn("request B about calendar", ctx_a)

        ctx_b = buf.as_claude_context(max_turns=10, session_key="B")
        self.assertIn("request B about calendar", ctx_b)
        self.assertNotIn("request A about emails", ctx_b)

    def test_new_thread_has_clean_slate(self):
        """A brand-new thread (no prior turns under its key) gets no preamble —
        request B never inherits request A's context."""
        from src.conversation import ConversationBuffer
        buf = ConversationBuffer()
        buf.add_user_text("#t-A", "old request", session_key="A")
        ctx = buf.as_claude_context(max_turns=10, session_key="new-thread")
        self.assertEqual(ctx, "")

    def test_unscoped_context_unchanged_for_voice(self):
        """No session_key -> whole-buffer behavior (voice continuity) intact."""
        from src.conversation import ConversationBuffer
        buf = ConversationBuffer()
        buf.add_user_text("#ucs", "hello", session_key="A")
        buf.add_aria_text("#ucs", "hi", session_key="B")
        ctx = buf.as_claude_context(max_turns=10)
        self.assertIn("hello", ctx)
        self.assertIn("hi", ctx)


# ---------------------------------------------------------------------------
# 3. The thread<->session binding round-trips (durable across restarts).
# ---------------------------------------------------------------------------

class TestThreadBinding(unittest.TestCase):
    def test_bind_and_lookup_roundtrip(self):
        import tempfile, os
        from src import db
        with tempfile.TemporaryDirectory() as d:
            with patch.object(db, "DB_PATH", os.path.join(d, "state.db")):
                db.init_db()
                self.assertIsNone(db.session_for_thread("999"))
                db.bind_thread("999", "999")
                self.assertEqual(db.session_for_thread("999"), "999")
                # Idempotent: re-bind keeps the original.
                db.bind_thread("999", "different")
                self.assertEqual(db.session_for_thread("999"), "999")


# ---------------------------------------------------------------------------
# 4. Spawn parity: text-Aria's agent loop can now call cursor_spawn /
#    cursor_agents (the capability that failure 146 needed).
# ---------------------------------------------------------------------------

class TestSpawnParity(unittest.TestCase):
    def test_cursor_spawn_and_agents_exposed_to_agent_loop(self):
        from src import tools
        schema_names = {s["name"] for s in tools._LOCAL_TOOL_SCHEMAS}
        self.assertIn("cursor_spawn", schema_names)
        self.assertIn("cursor_agents", schema_names)
        self.assertIn("cursor_spawn", tools._LOCAL_TOOL_HANDLERS)
        self.assertIn("cursor_agents", tools._LOCAL_TOOL_HANDLERS)

    def test_spawn_schema_requires_workspace_and_instruction(self):
        from src import tools
        spawn = next(s for s in tools._LOCAL_TOOL_SCHEMAS if s["name"] == "cursor_spawn")
        req = spawn["input_schema"]["required"]
        self.assertIn("workspace_root", req)
        self.assertIn("instruction", req)


# ---------------------------------------------------------------------------
# 5. Thread naming is mechanical, bounded, and never empty.
# ---------------------------------------------------------------------------

class TestThreadTitle(unittest.TestCase):
    def test_collapses_whitespace_and_caps(self):
        from src import bot
        title = bot._thread_title("  summarize\n\nmy   emails  from today  ")
        self.assertEqual(title, "summarize my emails from today")
        self.assertLessEqual(len(bot._thread_title("x" * 500)), bot._THREAD_NAME_MAX)

    def test_empty_falls_back(self):
        from src import bot
        self.assertEqual(bot._thread_title("   "), "Request")


# ---------------------------------------------------------------------------
# 6. End-to-end routing: a top-level message opens its own thread, runs the
#    loop with session_key == thread id, and posts ack + answer INTO the
#    thread — nothing leaks to the parent channel.
# ---------------------------------------------------------------------------

class TestHandleTextConversationThreads(unittest.IsolatedAsyncioTestCase):
    async def test_toplevel_message_opens_thread_and_isolates(self):
        from src import bot

        binds: dict[str, str] = {}

        thread = _fake_thread(999, "summarize my emails")
        msg = MagicMock(spec=discord.Message)
        msg.content = "summarize my emails"
        msg.attachments = []
        msg.channel = MagicMock(spec=discord.TextChannel)
        msg.channel.id = 100
        msg.channel.send = AsyncMock()
        msg.create_thread = AsyncMock(return_value=thread)

        captured = {}

        async def fake_handle_tool_call(name, args):
            captured["name"] = name
            captured["session_key"] = args.get("session_key")
            return "HERE IS YOUR ANSWER"

        with patch.object(bot, "gemini", None), \
             patch.object(bot, "bind_thread", lambda t, s: binds.__setitem__(t, s)), \
             patch("src.tools.handle_tool_call", side_effect=fake_handle_tool_call):
            await bot._handle_text_conversation(msg)

        # A thread was opened off the opener message.
        msg.create_thread.assert_awaited_once()
        # The loop ran under the THREAD's id, not the channel's.
        self.assertEqual(captured["session_key"], "999")
        self.assertEqual(captured["name"], "do_with_claude")
        # Ack + answer landed IN the thread.
        sent = " ".join(str(c.args[0]) for c in thread.send.await_args_list)
        self.assertIn("Got it", sent)
        self.assertIn("HERE IS YOUR ANSWER", sent)
        # Nothing leaked to the parent channel (the opener is the user's own msg).
        msg.channel.send.assert_not_called()
        # Binding recorded: thread id is its own session_key.
        self.assertEqual(binds.get("999"), "999")

    async def test_followup_inside_thread_reuses_session(self):
        """A message typed inside an existing thread continues that thread —
        no new thread, session_key stays the thread id."""
        from src import bot

        thread = _fake_thread(777, "existing thread")
        # The follow-up arrives with channel == the thread itself.
        msg = MagicMock(spec=discord.Message)
        msg.content = "actually also check calendar"
        msg.attachments = []
        msg.channel = thread
        msg.create_thread = AsyncMock()  # must NOT be called

        captured = {}

        async def fake_handle_tool_call(name, args):
            captured["session_key"] = args.get("session_key")
            return "ok"

        with patch.object(bot, "gemini", None), \
             patch.object(bot, "bind_thread", lambda t, s: None), \
             patch("src.tools.handle_tool_call", side_effect=fake_handle_tool_call):
            await bot._handle_text_conversation(msg)

        msg.create_thread.assert_not_called()
        self.assertEqual(captured["session_key"], "777")


# ---------------------------------------------------------------------------
# 7. Progress steps land in the request's thread (the ack stops lying).
# ---------------------------------------------------------------------------

class TestProgressIntoThread(unittest.IsolatedAsyncioTestCase):
    async def test_step_posts_into_thread_not_alerts(self):
        from src import bot

        thread = _fake_thread(999)
        alerts = MagicMock()
        alerts.send = AsyncMock()

        def fake_get_channel(cid):
            return thread if cid == 999 else alerts

        with patch.object(bot, "gemini", None), \
             patch.object(bot.bot, "get_channel", side_effect=fake_get_channel):
            await bot._emit_progress_to_user("→ checking email", session_key="999")

        thread.send.assert_awaited_once()
        self.assertIn("checking email", str(thread.send.await_args.args[0]))
        alerts.send.assert_not_called()

    async def test_step_without_thread_falls_to_alerts(self):
        from src import bot

        alerts = MagicMock()
        alerts.send = AsyncMock()

        with patch.object(bot, "gemini", None), \
             patch.object(bot.bot, "get_channel", return_value=alerts):
            await bot._emit_progress_to_user("→ working", session_key="")

        alerts.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
