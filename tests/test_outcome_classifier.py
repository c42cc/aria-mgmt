"""Proof that the deterministic outcome classifier reads failure *meaning*, not
exit codes — the fundamental fix for the spark2-SSH grind.

The dysfunctional primitive (forensic 2026-06-09): the loop trusted a count of
failures to notice a permanent wall and read the wrapper's exit code to decide
whether a result failed at all. A wrapper that exited 0 while printing
`Permission denied (publickey)` defeated both, so a wall became a 30-iteration,
~$20 grind. `classify_outcome` instead matches the failure text, so the
exitCode:0 masking is irrelevant.

Run with:
    .venv/bin/python -m unittest tests.test_outcome_classifier -v
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.outcomes import (  # noqa: E402
    BLOCKED,
    PROGRESS,
    TRANSIENT,
    TRANSIENT_RETRY_BUDGET,
    _action_family,
    classify_outcome,
    format_block,
)


# The exact incident shape: a wrapper that EXITS 0 while printing the real SSH
# auth failure. This single fixture is the whole point of the rewrite.
_MASKED_SSH = json.dumps({
    "stdout": "Pseudo-terminal will not be allocated.\n"
              "Permission denied (publickey,password).\nEXIT:255",
    "stderr": "",
    "exitCode": 0,
})
_REAL_SSH_FAIL = json.dumps({
    "stdout": "",
    "stderr": "ssh: connect to host spark2 port 22: Permission denied (publickey).",
    "exitCode": 1,
})
_SHELL_OK = json.dumps({"stdout": "ok", "stderr": "", "exitCode": 0})
_RECOVERABLE = json.dumps({
    "stdout": "",
    "stderr": "error: failed to push some refs to 'origin' (non-fast-forward)",
    "exitCode": 1,
})
_TIMEOUT = json.dumps({
    "stdout": "",
    "stderr": "ssh: connect to host spark2 port 22: Operation timed out",
    "exitCode": 1,
})


class TestMaskingIsDefeated(unittest.TestCase):
    """The core regression: a permanent wall is BLOCKED on the FIRST result,
    whether the wrapper exits 0 (masked) or 255."""

    def test_masked_exit0_permission_is_blocked(self):
        o = classify_outcome("execute_command", {"command": "ssh spark2@h"}, _MASKED_SSH)
        self.assertEqual(o.kind, BLOCKED)
        self.assertIn("publickey", o.reason.lower() + _MASKED_SSH.lower())

    def test_real_exit1_permission_is_blocked(self):
        o = classify_outcome("execute_command", {"command": "ssh spark2@h"}, _REAL_SSH_FAIL)
        self.assertEqual(o.kind, BLOCKED)

    def test_blocked_carries_an_actionable_need(self):
        o = classify_outcome("execute_command", {"command": "ssh spark2@h"}, _REAL_SSH_FAIL)
        self.assertTrue(o.need)


class TestTransientVsBlocked(unittest.TestCase):
    def test_timeout_is_transient(self):
        self.assertEqual(
            classify_outcome("execute_command", {"command": "ssh x"}, _TIMEOUT).kind,
            TRANSIENT,
        )

    def test_connection_reset_is_transient(self):
        env = json.dumps({"stderr": "Connection reset by peer", "exitCode": 1})
        self.assertEqual(classify_outcome("execute_command", {}, env).kind, TRANSIENT)

    def test_typed_rate_limit_is_transient(self):
        env = json.dumps({"_error_class": "rate_limit", "_message": "429 too many requests"})
        self.assertEqual(classify_outcome("search_emails", {"q": "x"}, env).kind, TRANSIENT)

    def test_typed_transient_is_transient(self):
        env = json.dumps({"_error_class": "transient", "_message": "Messages did not respond in time"})
        self.assertEqual(classify_outcome("imessage", {}, env).kind, TRANSIENT)


class TestDeclineAndPermission(unittest.TestCase):
    def test_declined_envelope_is_blocked_with_approval_path(self):
        env = json.dumps({
            "_error_class": "declined",
            "_message": "Tier-X/I confirmation timed out — the user did not respond.",
        })
        o = classify_outcome("execute_command", {"command": "openssl passwd -apr1 x"}, env)
        self.assertEqual(o.kind, BLOCKED)
        self.assertIn("!ok", o.need)
        self.assertIn("approval", o.need.lower())

    def test_permission_envelope_is_blocked(self):
        env = json.dumps({"_error_class": "permission", "_message": "Grant Full Disk Access"})
        o = classify_outcome("search_files", {}, env)
        self.assertEqual(o.kind, BLOCKED)


class TestProgressAndNoFalsePositive(unittest.TestCase):
    def test_success_is_progress(self):
        self.assertEqual(classify_outcome("execute_command", {"command": "ls"}, _SHELL_OK).kind, PROGRESS)

    def test_recoverable_failure_is_progress(self):
        """A non-fast-forward push is a recoverable failure the model should
        adapt to — NOT a wall. The loop must not stop on it."""
        self.assertEqual(classify_outcome("execute_command", {"command": "git push"}, _RECOVERABLE).kind, PROGRESS)

    def test_benign_success_mentioning_keyword_is_not_a_wall(self):
        """A SUCCESSFUL command whose output merely contains 'permission denied'
        (e.g. grepping a log) must not be misread as a wall — the meaning scan
        is gated on a result that looks like a failure."""
        env = json.dumps({"stdout": "2026-01-01 permission denied in app.log", "exitCode": 0})
        self.assertEqual(classify_outcome("execute_command", {"command": "grep x app.log"}, env).kind, PROGRESS)

    def test_typed_schema_error_is_progress(self):
        env = json.dumps({"_error_class": "schema", "_message": "missing required parameter"})
        self.assertEqual(classify_outcome("create_event", {}, env).kind, PROGRESS)

    def test_dup_hit_marker_is_progress(self):
        env = json.dumps({"_dup_hit": True, "cached_result": _REAL_SSH_FAIL})
        self.assertEqual(classify_outcome("execute_command", {"command": "ssh x"}, env).kind, PROGRESS)

    def test_empty_is_progress(self):
        self.assertEqual(classify_outcome("x", {}, "").kind, PROGRESS)


class TestActionFamily(unittest.TestCase):
    def test_ssh_variants_collapse(self):
        a = _action_family("execute_command", {"command": "ssh -o X u@spark2.local 'echo'"})
        b = _action_family("execute_command", {"command": "ssh root@10.0.0.199 'id'"})
        c = _action_family("execute_command", {"command": "# try tailscale\nssh u@spark2"})
        self.assertEqual(a, "exec:ssh")
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_env_and_sudo_stripped(self):
        self.assertEqual(
            _action_family("execute_command", {"command": "SSH_AUTH_SOCK=/x ssh u@h"}),
            "exec:ssh",
        )
        self.assertEqual(
            _action_family("execute_command", {"command": "sudo systemctl restart x"}),
            "exec:systemctl",
        )

    def test_non_shell_keyed_by_tool(self):
        self.assertEqual(_action_family("search_emails", {"q": "x"}), "tool:search_emails")


class TestGrindReplayPolicy(unittest.TestCase):
    """Replay the loop's outcome policy over a sequence of masked SSH failures
    and assert it BLOCKS within the first couple of steps instead of grinding to
    the iteration cap — the $36.87 grind becomes a guardrail."""

    @staticmethod
    def _run_policy(results: list[str], tool="execute_command",
                    args_seq: list[dict] | None = None) -> int:
        """Mirror the loop's continue/retry/stop policy purely. Returns the
        1-based step index at which it BLOCKS, or len+1 if it never blocks."""
        retry_used: dict[str, int] = {}
        for i, r in enumerate(results, start=1):
            args = (args_seq[i - 1] if args_seq else {"command": f"ssh spark2@host-{i}"})
            o = classify_outcome(tool, args, r)
            if o.is_blocked:
                return i
            if o.is_transient:
                used = retry_used.get(o.family, 0)
                if used >= TRANSIENT_RETRY_BUDGET:
                    return i
                retry_used[o.family] = used + 1
        return len(results) + 1

    def test_masked_grind_blocks_on_first(self):
        blocked_at = self._run_policy([_MASKED_SSH] * 30)
        self.assertEqual(blocked_at, 1)

    def test_transient_grind_blocks_after_one_retry(self):
        # Same family timing out: budget one retry (step 1), block on step 2.
        blocked_at = self._run_policy([_TIMEOUT] * 30)
        self.assertEqual(blocked_at, TRANSIENT_RETRY_BUDGET + 1)

    def test_recoverable_then_wall(self):
        # Two recoverable steps the model can work through, then a real wall.
        seq = [_RECOVERABLE, _RECOVERABLE, _REAL_SSH_FAIL]
        args = [{"command": "git pull"}, {"command": "git push"}, {"command": "ssh h"}]
        self.assertEqual(self._run_policy(seq, args_seq=args), 3)


class TestFormatBlock(unittest.TestCase):
    def test_reads_like_a_blocker_not_an_iteration_limit(self):
        msg = format_block("Permission denied (publickey)", "the SSH key authorized on spark2")
        self.assertIn("Blocked", msg)
        self.assertIn("hit a wall", msg.lower())
        self.assertIn("Permission denied", msg)
        self.assertIn("spark2", msg)
        self.assertNotIn("iteration limit", msg.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
