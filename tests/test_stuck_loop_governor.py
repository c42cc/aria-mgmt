"""Proof that the live agent loop stops at the first wall instead of grinding.

The dysfunctional primitive (forensic 2026-06-09, `#keys` thread): the loop's
governor counted failures and read exit codes, so a wrapper that exited 0 while
printing `Permission denied (publickey)` defeated it — a wall became a
30-iteration, ~$20, 6-minute grind ending in the spec-FAILED string "Task
reached iteration limit (30). Partial progress made."

The fix replaced the whole count-based governor with one deterministic
classifier (`src/outcomes.classify_outcome`) wired into the single result seam:
a permanent wall is BLOCKED on the FIRST occurrence (no threshold to defeat, no
exit code to mask), a transient gets one bounded retry per family, and a
recoverable failure stays PROGRESS so the model can work through it. These tests
drive the REAL `_do_with_claude_legacy` loop with a scripted fake model + MCP.

Run with:
    .venv/bin/python -m unittest tests.test_stuck_loop_governor -v
"""

from __future__ import annotations

import contextlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------
# Scriptable fakes for the model + MCP so the loop runs deterministically.
# --------------------------------------------------------------------------

class _Block:
    def __init__(self, type, name=None, input=None, id=None, text=None):
        self.type = type
        self.name = name
        self.input = input
        self.id = id
        self.text = text


class _Usage:
    def __init__(self, input_tokens=1000, output_tokens=20):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Resp:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _FakeMessages:
    """Returns scripted responses; once exhausted, repeats the last one (with a
    fresh, unique command) so a never-stopping model can't end the loop — only
    the outcome policy can."""

    def __init__(self, script, tail=None):
        self._script = list(script)
        self._tail = tail
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self._script:
            return self._script.pop(0)
        if self._tail is not None:
            return self._tail(self.calls)
        # Default tail: a unique failing ssh command every time.
        return _Resp([_Block(
            "tool_use", name="execute_command",
            input={"command": f"ssh spark2@host-{self.calls}"},
            id=f"t{self.calls}",
        )])


class _FakeClient:
    def __init__(self, script, tail=None):
        self.messages = _FakeMessages(script, tail)


class _FakeMcp:
    """Returns a configurable result for every tool call. `_tools` is read by
    `_build_context` (which we patch out) and `list_tools_anthropic` feeds the
    loop's tool list."""

    def __init__(self, result):
        self._result = result
        self._tools = {}

    def list_tools_anthropic(self):
        return []

    async def call_tool(self, name, args, session_key=""):
        if callable(self._result):
            return self._result(name, args)
        return self._result


# A real SSH auth failure (exitCode 1) and — the key fixture — the SAME failure
# MASKED behind an exitCode:0 wrapper, which the old governor could not see.
_SSH_FAIL = json.dumps({
    "stdout": "",
    "stderr": "Command failed: ssh: connect to host port 22: Permission denied (publickey).",
    "exitCode": 1,
})
_SSH_MASKED = json.dumps({
    "stdout": "Permission denied (publickey,password).\nEXIT:255",
    "stderr": "",
    "exitCode": 0,
})
_SSH_TIMEOUT = json.dumps({
    "stdout": "",
    "stderr": "ssh: connect to host spark2 port 22: Operation timed out",
    "exitCode": 1,
})
# A recoverable failure (non-fast-forward) the model should work through.
_GIT_RECOVERABLE = json.dumps({
    "stdout": "",
    "stderr": "error: failed to push some refs to 'origin' (non-fast-forward)",
    "exitCode": 1,
})
_SHELL_OK = json.dumps({"stdout": "ok", "stderr": "", "exitCode": 0})


@contextlib.contextmanager
def _patched_loop(client, mcp, *, cost_per_call=0.001):
    """Patch every external dependency of `_do_with_claude_legacy` so the loop
    runs purely on the scripted fakes (no Anthropic, no MCP, no DB, no disk)."""
    from src import tools
    tools._session_states.clear()
    with contextlib.ExitStack() as es:
        p = es.enter_context
        p(patch.object(tools, "_anthropic_client", client))
        p(patch("src.mcp.mcp_client", mcp))
        p(patch.object(tools, "mem_recall", lambda *a, **k: []))
        p(patch.object(tools, "load_template", lambda *a, **k: "SYSTEM"))
        p(patch.object(tools, "_build_context", lambda *a, **k: "<context/>\n"))
        p(patch.object(tools, "_emit_progress", AsyncMock()))
        p(patch.object(tools, "log_event", lambda *a, **k: None))
        p(patch.object(tools, "log_loop_execution", lambda *a, **k: None))
        p(patch.object(tools, "_estimate_cost", lambda i, o: cost_per_call))
        p(patch.object(tools, "_ground_check", AsyncMock(return_value=[])))
        yield tools


# --------------------------------------------------------------------------
# 1. A permanent wall stops the loop at the FIRST strike (not strike 3, not 30).
# --------------------------------------------------------------------------

class TestWallStopsImmediately(unittest.IsolatedAsyncioTestCase):
    async def test_permission_denied_blocks_on_first_call(self):
        client = _FakeClient([])  # default tail = unique failing ssh forever
        mcp = _FakeMcp(_SSH_FAIL)
        with _patched_loop(client, mcp) as t:
            result = await t._do_with_claude_legacy("ssh into spark2", "K")

        self.assertIn("Blocked", result)
        self.assertIn("hit a wall", result.lower())
        self.assertNotIn("iteration limit", result.lower())
        # First strike — no count threshold, nowhere near the 30-iteration cap.
        self.assertEqual(client.messages.calls, 1)

    async def test_masked_exit0_still_blocks_on_first_call(self):
        """THE regression: the wrapper exits 0, so the old exit-code governor
        saw success. The classifier reads the text and BLOCKS anyway."""
        client = _FakeClient([])
        mcp = _FakeMcp(_SSH_MASKED)
        with _patched_loop(client, mcp) as t:
            result = await t._do_with_claude_legacy("ssh into spark2", "K")

        self.assertIn("Blocked", result)
        self.assertEqual(client.messages.calls, 1)

    async def test_blocker_is_actionable(self):
        client = _FakeClient([])
        mcp = _FakeMcp(_SSH_FAIL)
        with _patched_loop(client, mcp) as t:
            result = await t._do_with_claude_legacy("ssh into spark2", "K")
        self.assertIn("Permission denied", result)
        self.assertIn("What I need to proceed", result)


# --------------------------------------------------------------------------
# 2. A transient gets exactly one bounded retry per family, then becomes a wall.
# --------------------------------------------------------------------------

class TestTransientRetry(unittest.IsolatedAsyncioTestCase):
    async def test_same_family_timeout_blocks_after_one_retry(self):
        from src import tools
        client = _FakeClient([])  # unique ssh each time, all time out (same family)
        mcp = _FakeMcp(_SSH_TIMEOUT)
        with _patched_loop(client, mcp) as t:
            result = await t._do_with_claude_legacy("ssh into spark2", "K")

        self.assertIn("Blocked", result)
        # One budgeted retry (call 1), then BLOCK on the second occurrence.
        self.assertEqual(client.messages.calls, tools.TRANSIENT_RETRY_BUDGET + 1)


# --------------------------------------------------------------------------
# 3. No false positives: a productive task with recoverable failures that then
#    succeeds must COMPLETE — the policy must not stop on a recoverable error.
# --------------------------------------------------------------------------

class TestNoFalsePositive(unittest.IsolatedAsyncioTestCase):
    async def test_recoverable_failures_then_success_completes(self):
        script = [
            _Resp([_Block("tool_use", name="execute_command",
                          input={"command": "git push"}, id="t1")]),
            _Resp([_Block("tool_use", name="execute_command",
                          input={"command": "git push --force-with-lease"}, id="t2")]),
            _Resp([_Block("text", text="DONE: deployed cleanly")],
                  stop_reason="end_turn"),
        ]
        client = _FakeClient(script)
        mcp = _FakeMcp(_GIT_RECOVERABLE)  # the two pushes fail recoverably, model recovers
        with _patched_loop(client, mcp) as t:
            result = await t._do_with_claude_legacy("deploy", "K")

        self.assertIn("DONE: deployed cleanly", result)
        self.assertNotIn("Blocked", result)
        self.assertNotIn("iteration limit", result.lower())


# --------------------------------------------------------------------------
# 4. Cost backstop: a non-stuck but expensive loop stops at the per-task dollar
#    ceiling before draining the daily cap — now expressed as a BLOCKED outcome.
# --------------------------------------------------------------------------

class TestCostBackstop(unittest.IsolatedAsyncioTestCase):
    async def test_cost_ceiling_pauses_loop(self):
        from src import tools
        # Each successful step "costs" $2; the $5 ceiling trips on the 3rd.
        client = _FakeClient([], tail=lambda n: _Resp([_Block(
            "tool_use", name="search_emails",
            input={"q": f"page-{n}"}, id=f"t{n}")]))
        mcp = _FakeMcp(_SHELL_OK)
        with _patched_loop(client, mcp, cost_per_call=2.0) as t:
            result = await t._do_with_claude_legacy("read everything", "K")

        self.assertIn("budget", result.lower())
        self.assertIn("Blocked", result)
        expected = int(tools._LOOP_COST_CAP_USD // 2.0) + 1
        self.assertEqual(client.messages.calls, expected)


# --------------------------------------------------------------------------
# 5. Input relevance: the ambient Cursor-watch firehose is excluded from a
#    focused request thread, but still reaches voice / global (no session_key).
# --------------------------------------------------------------------------

class TestCursorWatchScoping(unittest.TestCase):
    def test_watch_excluded_from_focused_thread(self):
        from src.conversation import ConversationBuffer
        buf = ConversationBuffer()
        buf.add_user_text("#keys", "set up tailscale on spark2", session_key="K")
        buf.add_cursor_event("[Cursor watch: live_visuals_4 produced an assistant turn]")
        ctx = buf.as_claude_context(max_turns=10, session_key="K")
        self.assertIn("set up tailscale on spark2", ctx)
        self.assertNotIn("live_visuals_4", ctx)

    def test_watch_included_for_voice_global(self):
        from src.conversation import ConversationBuffer
        buf = ConversationBuffer()
        buf.add_user_text("#ucs", "hi", session_key="K")
        buf.add_cursor_event("[Cursor watch: live_visuals_4 produced an assistant turn]")
        ctx = buf.as_claude_context(max_turns=10)  # no session_key == voice/global
        self.assertIn("live_visuals_4", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
