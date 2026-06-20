"""Operation A — the dispatch-boundary verified-done floor.

Proves the producer-side post-condition contract:
  - a state-changing verb whose artifact is provably ABSENT (a lag-free HARD
    failure) BLOCKS, and that block is mechanically a wall in src/outcomes.py;
  - anything that cannot be positively confirmed (verifier down, a lag-prone
    source, a timeout, a soft violation, or the anchor itself raising) is a
    loud UNCONFIRMED annotation — never a silent pass, never a false wall;
  - read tiers and un-anchored verbs are no-ops.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from src.anchors import postcondition
from src.anchors.base import AnchorReport


def _report(*, unverified: bool = False, hard: bool = False, soft: bool = False) -> AnchorReport:
    r = AnchorReport(tool="probe")
    r.unverified = unverified
    if hard:
        r.violate(7, "hard", "the expected artifact was not found")
    elif soft:
        r.violate(5, "soft", "a soft check did not pass")
    return r


class _FakeAnchor:
    def __init__(self, report=None, *, immediate=True, raises=None, sleep=0.0):
        self._report = report if report is not None else _report()
        self.immediate = immediate
        self._raises = raises
        self._sleep = sleep

    async def check(self, tool_call, aria_result):
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raises is not None:
            raise self._raises
        return self._report

    async def health_check(self):
        return True


class PostconditionFloor(unittest.IsolatedAsyncioTestCase):
    async def test_read_tier_is_noop(self):
        # A read verb never touches the anchor path — short-circuits on tier.
        with patch.object(postcondition, "anchor_for", side_effect=AssertionError("must not look up")):
            v = await postcondition.evaluate("read_file", "R", {}, "ok")
        self.assertEqual(v.decision, postcondition.PASS)

    async def test_no_anchor_passes(self):
        with patch.object(postcondition, "anchor_for", return_value=None):
            v = await postcondition.evaluate("delete_thing", "I", {}, "done")
        self.assertEqual(v.decision, postcondition.PASS)

    async def test_correct_passes(self):
        anchor = _FakeAnchor(_report())  # no violations, not unverified
        with patch.object(postcondition, "anchor_for", return_value=anchor):
            v = await postcondition.evaluate("write_file", "W", {"path": "/x"}, "wrote")
        self.assertEqual(v.decision, postcondition.PASS)
        self.assertEqual(v.binary, "correct")

    async def test_failed_immediate_blocks(self):
        anchor = _FakeAnchor(_report(hard=True), immediate=True)
        with patch.object(postcondition, "anchor_for", return_value=anchor):
            v = await postcondition.evaluate("write_file", "W", {"path": "/x"}, "wrote")
        self.assertEqual(v.decision, postcondition.BLOCK)
        self.assertEqual(v.binary, "failed")
        self.assertIn("write_file", v.message)
        self.assertIn("did not land", v.message)

    async def test_failed_non_immediate_annotates(self):
        # Lag-prone source (e.g. Gmail Sent index): loud, but never a false wall.
        anchor = _FakeAnchor(_report(hard=True), immediate=False)
        with patch.object(postcondition, "anchor_for", return_value=anchor):
            v = await postcondition.evaluate("send_email", "I", {"to": "a@b"}, "sent")
        self.assertEqual(v.decision, postcondition.ANNOTATE)
        self.assertEqual(v.binary, "failed")
        self.assertIn("UNCONFIRMED", v.annotation)

    async def test_unverified_annotates(self):
        anchor = _FakeAnchor(_report(unverified=True))
        with patch.object(postcondition, "anchor_for", return_value=anchor):
            v = await postcondition.evaluate("create_event", "W", {}, "created")
        self.assertEqual(v.decision, postcondition.ANNOTATE)
        self.assertEqual(v.binary, "unverified")

    async def test_degraded_annotates(self):
        anchor = _FakeAnchor(_report(soft=True))
        with patch.object(postcondition, "anchor_for", return_value=anchor):
            v = await postcondition.evaluate("write_file", "W", {"path": "/x"}, "wrote")
        self.assertEqual(v.decision, postcondition.ANNOTATE)
        self.assertEqual(v.binary, "degraded")

    async def test_anchor_exception_is_unconfirmed(self):
        # A broken verifier reads as UNCONFIRMED, never crashes the dispatch.
        anchor = _FakeAnchor(raises=RuntimeError("boom"))
        with patch.object(postcondition, "anchor_for", return_value=anchor):
            v = await postcondition.evaluate("write_file", "W", {"path": "/x"}, "wrote")
        self.assertEqual(v.decision, postcondition.ANNOTATE)
        self.assertEqual(v.binary, "unverified")

    async def test_timeout_is_unconfirmed(self):
        anchor = _FakeAnchor(_report(), sleep=0.3)
        with patch.object(postcondition, "anchor_for", return_value=anchor), \
             patch.object(postcondition, "_POSTCOND_TIMEOUT_SEC", 0.05):
            v = await postcondition.evaluate("write_file", "W", {"path": "/x"}, "wrote")
        self.assertEqual(v.decision, postcondition.ANNOTATE)
        self.assertEqual(v.binary, "unverified")


class FailedPostconditionIsAWall(unittest.TestCase):
    """The BLOCK verdict must terminate as a mechanical wall in the loop."""

    def test_outcomes_maps_unverified_envelope_to_blocked(self):
        from src import mcp
        from src.outcomes import classify_outcome, BLOCKED

        envelope = mcp._typed_error(
            mcp.ERR_UNVERIFIED,
            "the write_file post-condition FAILED — file absent. The change did not land.",
            "file absent",
        )
        outcome = classify_outcome("write_file", {"path": "/x"}, envelope)
        self.assertEqual(outcome.kind, BLOCKED)
        self.assertTrue(outcome.need)


if __name__ == "__main__":
    unittest.main(verbosity=2)
