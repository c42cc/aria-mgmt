"""Proof that the agent loop's *progress governor* behaves.

The dysfunctional primitive (forensic 2026-06-09, `#keys` thread): the loop's
only governor was a blunt iteration cap. With no progress signal it could not
perceive that it was *stuck* — repeating one kind of failing action against an
immovable wall (SSH into a host whose Tailscale identity we don't hold). So a
wall became a 30-iteration, ~$20, 6-minute grind that ended in the spec-FAILED
string "Task reached iteration limit (30). Partial progress made." — and along
the way it brute-forced guessed SSH passwords. The byte-identical dedup ledger
never fired because every command was textually unique.

The fix generalizes the existing *decline*-abort (which only triggers when the
user refuses a tier-X/I command) into a *failure*-abort: count failing tool
results per action-family and in total, and stop early with an actionable
blocker. A per-loop cost ceiling backstops the rare expensive-but-not-stuck
case. These tests drive the REAL `_do_with_claude_legacy` loop with a scripted
fake model + MCP, and prove it now stops early instead of grinding.

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
    the governor can."""

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


_SHELL_FAIL = json.dumps({
    "stdout": "",
    "stderr": "Command failed: ssh: connect to host port 22: Permission denied (publickey).",
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


def _run(tools, task="do the thing", session_key="K"):
    import asyncio
    return asyncio.run(tools._do_with_claude_legacy(task, session_key))


# --------------------------------------------------------------------------
# 1. The loop stops EARLY on a wall instead of grinding to the iteration cap.
# --------------------------------------------------------------------------

class TestStuckAbort(unittest.IsolatedAsyncioTestCase):
    async def test_same_family_failures_abort_fast(self):
        """The spark2-SSH grind: the model keeps emitting (textually unique)
        ssh commands that all fail. The per-family threshold trips at 3 and
        the loop returns an actionable blocker — not the iteration-limit
        string, and nowhere near 30 iterations."""
        from src import tools
        client = _FakeClient([])  # default tail = unique failing ssh forever
        mcp = _FakeMcp(_SHELL_FAIL)
        with _patched_loop(client, mcp) as t:
            result = await t._do_with_claude_legacy("ssh into spark2", "K")

        self.assertIn("Blocked", result)
        self.assertIn("hit a wall", result.lower())
        self.assertNotIn("iteration limit", result.lower())
        # Aborted at the per-family threshold (3), not the 30-iteration cap.
        self.assertEqual(client.messages.calls, tools._STUCK_PER_FAMILY_ABORT)
        self.assertLess(client.messages.calls, 10)

    async def test_total_failures_across_families_abort(self):
        """A thrash that sprays across DIFFERENT verbs never trips the
        per-family counter, but the total-failure backstop still stops it."""
        verbs = ["ssh", "ping", "curl", "nc", "nmap", "dig", "telnet", "scp"]
        script = [
            _Resp([_Block("tool_use", name="execute_command",
                          input={"command": f"{v} spark2"}, id=f"t{i}")])
            for i, v in enumerate(verbs)
        ]
        client = _FakeClient(script)
        mcp = _FakeMcp(_SHELL_FAIL)
        with _patched_loop(client, mcp) as t:
            result = await t._do_with_claude_legacy("probe spark2 every way", "K")

        self.assertIn("Blocked", result)
        self.assertEqual(client.messages.calls, t._STUCK_TOTAL_ABORT)

    async def test_blocker_names_the_failing_family(self):
        from src import tools
        client = _FakeClient([])
        mcp = _FakeMcp(_SHELL_FAIL)
        with _patched_loop(client, mcp) as t:
            result = await t._do_with_claude_legacy("ssh into spark2", "K")
        self.assertIn("exec:ssh", result)
        self.assertIn("Permission denied", result)


# --------------------------------------------------------------------------
# 2. No false positives: a productive task with a couple of failures that
#    then succeeds must COMPLETE normally — the governor must not abort it.
# --------------------------------------------------------------------------

class TestNoFalsePositive(unittest.IsolatedAsyncioTestCase):
    async def test_two_failures_then_success_completes(self):
        script = [
            _Resp([_Block("tool_use", name="execute_command",
                          input={"command": "git push"}, id="t1")]),
            _Resp([_Block("tool_use", name="execute_command",
                          input={"command": "python build.py"}, id="t2")]),
            _Resp([_Block("text", text="DONE: deployed cleanly")],
                  stop_reason="end_turn"),
        ]
        client = _FakeClient(script)
        mcp = _FakeMcp(_SHELL_FAIL)  # the two tool calls fail, model recovers
        with _patched_loop(client, mcp) as t:
            result = await t._do_with_claude_legacy("deploy", "K")

        self.assertIn("DONE: deployed cleanly", result)
        self.assertNotIn("Blocked", result)
        self.assertNotIn("iteration limit", result.lower())


# --------------------------------------------------------------------------
# 3. Cost backstop: a non-stuck but expensive loop stops at the per-task
#    dollar ceiling before draining the daily cap.
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
        expected = int(tools._LOOP_COST_CAP_USD // 2.0) + 1
        self.assertEqual(client.messages.calls, expected)


# --------------------------------------------------------------------------
# 4. Failure/family/reason primitives (pure functions).
# --------------------------------------------------------------------------

class TestFailurePrimitives(unittest.TestCase):
    def test_is_failed_result_shell_nonzero(self):
        from src.tools import _is_failed_result
        self.assertTrue(_is_failed_result(_SHELL_FAIL))

    def test_is_failed_result_shell_zero_is_not_failure(self):
        from src.tools import _is_failed_result
        self.assertFalse(_is_failed_result(_SHELL_OK))

    def test_is_failed_result_typed_envelope(self):
        from src.tools import _is_failed_result
        env = json.dumps({"_error_class": "permission", "_message": "no FDA"})
        self.assertTrue(_is_failed_result(env))

    def test_is_failed_result_plain_and_empty(self):
        from src.tools import _is_failed_result
        self.assertFalse(_is_failed_result(""))
        self.assertFalse(_is_failed_result("here are your 3 emails"))

    def test_is_failed_result_wrapped_textcontent(self):
        from src.tools import _is_failed_result
        wrapped = '[TextContent(text=\'{"exitCode": 1, "stderr": "boom"}\')]'
        self.assertTrue(_is_failed_result(wrapped))

    def test_action_family_collapses_ssh_variants(self):
        from src.tools import _action_family
        a = _action_family("execute_command", {"command": "ssh -o X u@spark2.local 'echo'"})
        b = _action_family("execute_command", {"command": "ssh root@10.0.0.199 'id'"})
        c = _action_family("execute_command", {"command": "# try tailscale\nssh u@spark2"})
        self.assertEqual(a, "exec:ssh")
        self.assertEqual(a, b)
        self.assertEqual(a, c)  # comment line stripped before the verb

    def test_action_family_strips_env_and_sudo(self):
        from src.tools import _action_family
        self.assertEqual(
            _action_family("execute_command", {"command": "SSH_AUTH_SOCK=/x ssh u@h"}),
            "exec:ssh",
        )
        self.assertEqual(
            _action_family("execute_command", {"command": "sudo systemctl restart x"}),
            "exec:systemctl",
        )

    def test_action_family_non_shell_is_tool_keyed(self):
        from src.tools import _action_family
        self.assertEqual(_action_family("search_emails", {"q": "x"}), "tool:search_emails")

    def test_short_failure_reason_prefers_stderr(self):
        from src.tools import _short_failure_reason
        self.assertIn("Permission denied", _short_failure_reason(_SHELL_FAIL))
        env = json.dumps({"_error_class": "permission", "_message": "grant Full Disk Access"})
        self.assertIn("Full Disk Access", _short_failure_reason(env))


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
