"""The Hands endpoint: Aria dispatches a build cell to a Spark node and judges it
by GROUND TRUTH (a real commit on the branch), never the cell's narration.

Offline: the node calls (run_audit/run_status/fetch_results) are mocked so the
routing + verdict logic is locked deterministically. The live path is proven by
a real dispatch (see the run log)."""

from __future__ import annotations

import time

from src import dispatcher, loops, spark


def _loop():
    return loops.load_loops()["hands-build"]


def test_hands_delivers_on_a_real_commit(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(spark, "run_audit", lambda *a, **k: {"ok": True, "run_id": "r1", "branch": "hands/x"})
    monkeypatch.setattr(spark, "run_status", lambda *a, **k: {
        "done": True, "commits_on_branch": "1", "exit_code": 0,
        "result": {"cost_usd": 0.04}, "last_assistant": "done",
    })
    monkeypatch.setattr(spark, "fetch_results", lambda *a, **k: {"bundle_path": "/tmp/x.bundle"})
    r = dispatcher.run(_loop(), {"task": "add a file"})
    assert r.delivered is True
    assert "commits=1" in r.summary
    assert r.broke is None


def test_hands_not_delivered_when_cell_left_no_diff(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(spark, "run_audit", lambda *a, **k: {"ok": True, "run_id": "r2", "branch": "hands/y"})
    monkeypatch.setattr(spark, "run_status", lambda *a, **k: {
        "done": True, "commits_on_branch": "0", "exit_code": 0, "result": {}, "last_assistant": "",
    })
    monkeypatch.setattr(spark, "fetch_results", lambda *a, **k: {"bundle_path": "(none)"})
    r = dispatcher.run(_loop(), {"task": "do nothing"})
    assert r.delivered is False
    assert "no diff" in (r.broke or "")


def test_hands_loud_on_launch_failure(monkeypatch):
    monkeypatch.setattr(spark, "run_audit", lambda *a, **k: {"ok": False, "error": "billing='api' but no key on node"})
    r = dispatcher.run(_loop(), {"task": "x"})
    assert r.delivered is False
    assert "no key" in (r.broke or "")


def test_hands_loud_on_timeout(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(spark, "run_audit", lambda *a, **k: {"ok": True, "run_id": "r3", "branch": "hands/z"})
    monkeypatch.setattr(spark, "run_status", lambda *a, **k: {"done": False})
    # Drive the clock past the deadline: 1st call computes it, 2nd is already past.
    clock = iter([1000.0, 1e12, 1e12])
    monkeypatch.setattr(time, "time", lambda: next(clock))
    r = dispatcher.run(_loop(), {"task": "x"})
    assert r.delivered is False
    assert "did not finish" in (r.broke or "")
