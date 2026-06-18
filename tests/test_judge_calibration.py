"""Step 6 lock: the judge earns the right to gate.

Pure-logic tests for the two calibration gates (agreement + separation), the
refuse-to-trust freshness check, and — the one that matters — a LENIENT judge
(scores everything 'correct') MUST fail calibration, so it can never be trusted
to gate a Task's done claim. The live calibration run (real API) is `make
eval-calibrate`.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import judge_calibration as cal  # noqa: E402


def _r(label, verdict, score):
    return {"id": label, "label": label, "verdict": verdict, "score": score}


def test_perfect_judge_passes():
    results = [
        _r("good", "correct", 1.0),
        _r("good", "correct", 0.95),
        _r("bad", "failed", 0.0),
        _r("bad", "degraded", 0.4),
    ]
    out = cal.evaluate_calibration(results)
    assert out["agreement"] == 1.0
    assert out["separation"] is True
    assert out["passed"] is True


def test_lenient_judge_fails_calibration():
    # A judge that calls everything 'correct'/1.0 — the false-green machine.
    results = [
        _r("good", "correct", 1.0),
        _r("good", "correct", 1.0),
        _r("bad", "correct", 1.0),
        _r("bad", "correct", 1.0),
    ]
    out = cal.evaluate_calibration(results)
    assert out["agreement"] < cal.AGREEMENT_MIN   # the bad ones disagree
    assert out["separation"] is False              # min(good)=1.0 !> max(bad)=1.0
    assert out["passed"] is False


def test_separation_requires_both_sides():
    # All good, no bad — separation is vacuous and must NOT pass.
    results = [_r("good", "correct", 1.0), _r("good", "correct", 0.9)]
    assert cal.compute_separation(results) is False


def test_separation_fails_when_overlap():
    results = [_r("good", "correct", 0.6), _r("bad", "failed", 0.6)]
    assert cal.compute_separation(results) is False
    results = [_r("good", "correct", 0.8), _r("bad", "failed", 0.3)]
    assert cal.compute_separation(results) is True


def test_label_matches_verdict():
    assert cal.label_matches_verdict("good", "correct") is True
    assert cal.label_matches_verdict("good", "degraded") is False
    assert cal.label_matches_verdict("bad", "failed") is True
    assert cal.label_matches_verdict("bad", "unverified") is True
    assert cal.label_matches_verdict("bad", "correct") is False


def test_is_calibrated_refuses_stale_and_failed(monkeypatch):
    now = datetime(2026, 6, 18, tzinfo=timezone.utc)

    def fresh_passing():
        return {"passed": True, "ts": (now - timedelta(days=2)).isoformat()}

    def stale_passing():
        return {"passed": True, "ts": (now - timedelta(days=99)).isoformat()}

    def fresh_failing():
        return {"passed": False, "ts": now.isoformat()}

    monkeypatch.setattr(cal, "latest_calibration", fresh_passing)
    assert cal.is_calibrated(now=now) is True

    monkeypatch.setattr(cal, "latest_calibration", stale_passing)
    assert cal.is_calibrated(now=now) is False

    monkeypatch.setattr(cal, "latest_calibration", fresh_failing)
    assert cal.is_calibrated(now=now) is False

    monkeypatch.setattr(cal, "latest_calibration", lambda: None)
    assert cal.is_calibrated(now=now) is False
