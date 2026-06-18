"""Unit tests for the live-outcome meter's pure logic (scripts/live_meter.py).

The full live run (real Claude + MCP) is exercised at the base-green checkpoint
on the trunk; here we lock the classification + receipt logic so a blocker or a
no-tool 'answer' can never be mistaken for a genuine success.
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_REPO, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import live_meter  # noqa: E402


def test_blocked_result_is_not_success():
    blocked = (
        "**Blocked — I hit a wall and stopped instead of grinding.**\n"
        "What failed: authentication failed\n"
        "What I need to proceed: a credential."
    )
    ok, reason = live_meter.classify_meter_result(blocked, tool_fired=True)
    assert ok is False
    assert "blocked" in reason.lower()


def test_empty_result_is_not_success():
    ok, reason = live_meter.classify_meter_result("   ", tool_fired=True)
    assert ok is False
    assert "empty" in reason.lower()


def test_no_tool_fired_is_not_success():
    # A plausible-sounding answer with no real action is exactly the failure mode
    # the meter exists to catch (talk, not done).
    ok, reason = live_meter.classify_meter_result(
        "There are 3 allowed directories.", tool_fired=False
    )
    assert ok is False
    assert "no tool" in reason.lower()


def test_genuine_success():
    ok, reason = live_meter.classify_meter_result(
        "You have 3 allowed directories.", tool_fired=True
    )
    assert ok is True
    assert "genuine" in reason.lower()


def test_receipt_maps_verdict_and_marks_certification():
    good = live_meter.build_receipt(
        build_hash="abc123",
        head_sha="deadbeef",
        dirty=False,
        branch="main",
        certifies_trunk=True,
        task="t",
        result="ok",
        tool_fired=True,
        ok=True,
        verdict_reason="genuine success",
    )
    assert good["verdict"] == "correct"
    assert good["certifies_trunk"] is True
    assert good["build_hash"] == "abc123"

    bad = live_meter.build_receipt(
        build_hash="abc123",
        head_sha="deadbeef",
        dirty=True,
        branch="fix/x",
        certifies_trunk=False,
        task="t",
        result="**Blocked …",
        tool_fired=False,
        ok=False,
        verdict_reason="blocked",
    )
    assert bad["verdict"] == "failed"
    assert bad["certifies_trunk"] is False
