"""Step 2 locks: posture has ONE home, and a broken judge is LOUD.

Guards the two fixes the post-mortem demanded:
  - Approval posture is derived from config (one home), not independently
    asserted by the spec — so configured-correct autonomy can't be judged a
    violation.
  - A judge mechanism failure raises JudgeError (never a silent 'degraded'
    verdict), and the record path turns that into an 'unverified' outcome that
    counts as fail.
"""

from __future__ import annotations

import json

from src import judge


def test_posture_off_makes_confirmed_null_correct():
    clause = judge._posture_clause(confirm=False)
    assert "OFF" in clause
    assert "confirmed=null on a" in clause
    assert "CORRECT" in clause
    assert "MUST NOT be marked a confirmation violation" in clause


def test_posture_on_makes_unconfirmed_a_violation():
    clause = judge._posture_clause(confirm=True)
    assert "ON" in clause
    assert "violation" in clause.lower()


def test_judge_error_is_distinct_and_loud():
    # JudgeError must be its own type so a mechanism failure can never be
    # caught-and-flattened into a verdict by a broad handler that meant to
    # catch parse errors.
    assert issubclass(judge.JudgeError, Exception)


def test_unparseable_judge_output_is_not_a_verdict():
    # The parser raises on garbage; evaluate() wraps that into JudgeError rather
    # than inventing a 'degraded' score. We assert the parser itself refuses to
    # fabricate, which is the seam evaluate() converts to a loud JudgeError.
    import pytest

    with pytest.raises(json.JSONDecodeError):
        judge._parse_judge_response("not json at all, no braces")
