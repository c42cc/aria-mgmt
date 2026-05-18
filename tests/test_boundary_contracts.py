"""Regression tests for the Boundary Contracts unification.

Each test replays a previously-failing production session pattern as a
read-only fixture and asserts the new gate, classifier, quarantine, or
anchor would now intercept it. No live APIs are called.

Run with:
    .venv/bin/python -m unittest tests.test_boundary_contracts -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# `src.bot` constructs a Discord client at module load, which calls
# `asyncio.get_event_loop()`. Import it once now, behind a held loop, so
# subsequent `asyncio.run(...)` calls in individual tests don't disturb
# the already-loaded module.
asyncio.set_event_loop(asyncio.new_event_loop())
from src import bot as _bot_module  # noqa: E402,F401  (eager import)


class Session24_AnthropicToolNameRegex(unittest.TestCase):
    """Session 24: Anthropic 400 because a registered tool name contained a dot."""

    def test_tool_name_regex_assertion_at_anthropic_boundary(self):
        from src.mcp import _VALID_TOOL_NAME, _sanitize_tool_name
        offending = "apple.mail.messages"
        self.assertFalse(_VALID_TOOL_NAME.match(offending))
        safe = _sanitize_tool_name(offending)
        self.assertTrue(_VALID_TOOL_NAME.match(safe))

    def test_probe_catches_unsanitised_name_at_boot(self):
        from src.preflight import probe_mcp_tool_name_regex

        class _StubClient:
            _started = True
            _tools = {"good_name": {}, "apple.mail.messages": {}}

        ok, err, _fix, _det = asyncio.run(probe_mcp_tool_name_regex(_StubClient()))
        self.assertFalse(ok)
        self.assertIn("Anthropic regex", err)


class Session30_PermissionPassedAsData(unittest.TestCase):
    """Session 30: agent paraphrased an OAuth error as if it were data ("no emails today")."""

    def test_oauth_failure_classified_as_permission(self):
        from src.mcp import ERR_PERMISSION, _classify_error_text
        text = "Gmail API returned an authentication error: not authorized"
        self.assertEqual(_classify_error_text(text), ERR_PERMISSION)

    def test_typed_envelope_distinguishes_from_normal_result(self):
        from src.mcp import ERR_PERMISSION, _typed_error
        env = json.loads(_typed_error(ERR_PERMISSION, "msg", "raw"))
        self.assertEqual(env["_error_class"], "permission")
        self.assertIn("_hint", env)


class Session40_GmailRateLimitStorm(unittest.TestCase):
    """Session 40: 9 search_emails in <20s → 429 storm → fabricated 22 each."""

    def test_local_token_bucket_caps_storm(self):
        from src.mcp import _TokenBucket
        bucket = _TokenBucket(rate=3.0)
        outcomes = [bucket.try_acquire() for _ in range(5)]
        self.assertEqual(outcomes, [True, True, True, False, False])

    def test_429_text_classified_as_rate_limit(self):
        from src.mcp import ERR_RATE_LIMIT, _classify_error_text
        text = (
            "Error: Quota exceeded for quota metric 'Queries' and limit "
            "'Queries per minute per user' of service 'gmail.googleapis.com'"
        )
        self.assertEqual(_classify_error_text(text), ERR_RATE_LIMIT)


class Session41_CountFabrication(unittest.TestCase):
    """Session 41: claimed 18 Python files, ground truth 29. Anchor floor caught it post-hoc."""

    def test_violations_summary_only_nonempty_on_degraded_or_failed(self):
        from src.tools import _summarize_anchor_violations
        correct = [{"tool": "x", "binary": "correct", "violations": [], "facts": []}]
        unverified = [{"tool": "x", "binary": "correct", "unverified": True, "violations": [], "facts": []}]
        failed = [{
            "tool": "filesystem.search_files",
            "binary": "failed",
            "violations": [{"prop": 3, "severity": "hard", "detail": "Aria claimed 18, ground truth is 29"}],
            "facts": [
                {"key": "ground_truth_count", "value": 29, "source": "glob.glob"},
                {"key": "aria_claimed_count", "value": 18, "source": "aria_result_extraction"},
            ],
        }]
        self.assertEqual(_summarize_anchor_violations(correct), "")
        self.assertEqual(_summarize_anchor_violations(unverified), "")
        body = _summarize_anchor_violations(failed)
        self.assertIn("filesystem.search_files", body)
        self.assertIn("29", body)
        self.assertIn("18", body)

    def test_ground_check_returns_empty_when_no_trace(self):
        from src.tools import _ground_check
        result = asyncio.run(_ground_check([], "no trace yet", session_key="t"))
        self.assertEqual(result, "")


class Session46_ReentrancyLeak(unittest.TestCase):
    """Session 46: agent-loop re-entrancy lock error returned as Aria's reply."""

    def test_quarantine_matches_reentrancy_error(self):
        from src.bot import _looks_like_control_plane_error
        msg = '{"error": "An agent loop is already running for this session. Wait for it to finish or use !stop."}'
        self.assertTrue(_looks_like_control_plane_error(msg))

    def test_quarantine_matches_anthropic_raw(self):
        from src.bot import _looks_like_control_plane_error
        msg = "Error code: 400 - {'error': {'message': 'tools.36.custom.name: ...'}}"
        self.assertTrue(_looks_like_control_plane_error(msg))

    def test_quarantine_lets_typed_tool_errors_through(self):
        from src.bot import _looks_like_control_plane_error
        msg = '{"_error_class": "permission", "_message": "FDA missing", "_hint": "...", "_raw": "..."}'
        self.assertFalse(_looks_like_control_plane_error(msg))

    def test_quarantine_lets_real_replies_through(self):
        from src.bot import _looks_like_control_plane_error
        self.assertFalse(
            _looks_like_control_plane_error("Here is a summary of your emails today.")
        )


class Session48_DateAndCount(unittest.TestCase):
    """Session 48: get-current-time errored, Aria guessed May 12; count claim drifted (60 vs 63)."""

    def test_context_block_carries_current_date(self):
        from src.tools import _build_context
        block = _build_context(session_key="test-48")
        self.assertIn("<context>", block)
        self.assertIn("now:", block)
        self.assertIn("America/Los_Angeles", block)
        self.assertIn("primary_mail_source: gmail", block)

    def test_unknown_action_classified_as_schema(self):
        """list / read schema mismatch (related F5 root cause)."""
        from src.mcp import ERR_SCHEMA, _classify_error_text
        self.assertEqual(_classify_error_text("Unknown mail_messages action: list"), ERR_SCHEMA)


class PlanCitationAnchor_F14(unittest.TestCase):
    """Plan extension: invented module citations (sessions 8/13/14)."""

    def test_invented_paths_fail(self):
        from src.anchors.plan_citation import PlanCitationAnchor
        text = (
            "Use src/spotify_handler.py to implement the SpotifyManager class "
            "from lib/spotify_sdk.py."
        )
        rep = asyncio.run(
            PlanCitationAnchor().check({"tool": "plan_with_claude", "result": text}, text)
        )
        self.assertEqual(rep.binary, "failed")

    def test_paths_marked_new_pass(self):
        from src.anchors.plan_citation import PlanCitationAnchor
        text = (
            "I will create a new src/spotify_handler.py (to be created) "
            "to hold the logic."
        )
        rep = asyncio.run(
            PlanCitationAnchor().check({"tool": "plan_with_claude", "result": text}, text)
        )
        self.assertEqual(rep.binary, "correct")

    def test_real_paths_pass(self):
        from src.anchors.plan_citation import PlanCitationAnchor
        text = "Update src/tools.py and src/bot.py."
        rep = asyncio.run(
            PlanCitationAnchor().check({"tool": "plan_with_claude", "result": text}, text)
        )
        self.assertEqual(rep.binary, "correct")

    def test_judge_synthesises_trace_for_plans(self):
        """Plan sessions have empty trace; _run_anchors must synth one."""
        from src.judge import _run_anchors

        record = {
            "tool_name": "plan_with_claude",
            "product": "planning",
            "context_json": json.dumps({"tool_trace": []}),
            "outputs_json": json.dumps({"result": "Edit src/spotify_handler.py please."}),
        }
        reports = asyncio.run(_run_anchors(record))
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["tool"], "plan_with_claude")
        self.assertEqual(reports[0]["binary"], "failed")


class ArgsSchemaValidation_F5(unittest.TestCase):
    """Pre-dispatch schema check intercepts F5-style "Unknown action" before it reaches the server."""

    def test_unknown_enum_value_caught(self):
        from src.mcp import _validate_args_against_schema
        schema = {
            "type": "object",
            "required": ["action"],
            "properties": {"action": {"type": "string", "enum": ["read", "search"]}},
        }
        err = _validate_args_against_schema({"action": "list"}, schema)
        self.assertIsNotNone(err)
        self.assertIn("list", err)
        self.assertIn("'read', 'search'", err)

    def test_missing_required_caught(self):
        from src.mcp import _validate_args_against_schema
        schema = {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}}
        err = _validate_args_against_schema({}, schema)
        self.assertIsNotNone(err)
        self.assertIn("query", err)

    def test_valid_call_passes(self):
        from src.mcp import _validate_args_against_schema
        schema = {
            "type": "object",
            "required": ["action"],
            "properties": {"action": {"type": "string", "enum": ["read"]}, "limit": {"type": "integer"}},
        }
        self.assertIsNone(_validate_args_against_schema({"action": "read", "limit": 3}, schema))


if __name__ == "__main__":
    unittest.main()
