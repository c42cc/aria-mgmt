"""Per-thread live Cursor status (forensic 2026-06-16).

The bug: Aria reported active-thread state from a Haiku guess over stale
transcript files, and the live registry tracked status per-WORKSPACE, so one
thread aborting flipped the whole workspace to "finished" while a sibling was
actively running. The fix: per-THREAD status from the live hook stream, with
recency, so "what's underway now?" is ground truth.
"""

from __future__ import annotations

import time

from src.cursor_registry import (
    RUNNING_RECENCY_SEC,
    CursorAgent,
    CursorAgentRegistry,
    SessionInfo,
    _aggregate_agent_status,
    _session_live,
)


def _sess(sid: str, status: str, age_sec: float) -> SessionInfo:
    now = time.time()
    return SessionInfo(
        sid=sid, started_at=now - 1000, last_event_at=now - age_sec, status=status
    )


def _agent(sessions: list[SessionInfo]) -> CursorAgent:
    a = CursorAgent(agent_id="/ws", workspace_root="/ws", project_label="ws")
    for s in sessions:
        a.sessions[s.sid] = s
    return a


def test_session_live_recency():
    now = time.time()
    assert _session_live(_sess("a", "running", 5), now) is True
    assert _session_live(_sess("a", "running", RUNNING_RECENCY_SEC + 60), now) is False
    assert _session_live(_sess("a", "finished", 1), now) is False


def test_aggregate_running_if_any_thread_live():
    # THE anti-flip case: one thread aborted (finished), a sibling is actively
    # running -> the workspace must report running, not finished.
    a = _agent([_sess("aborted", "finished", 2), _sess("active", "running", 3)])
    assert _aggregate_agent_status(a, time.time()) == "running"


def test_aggregate_all_idle_is_not_running():
    a = _agent([_sess("x", "finished", 2), _sess("y", "finished", 5)])
    assert _aggregate_agent_status(a, time.time()) == "finished"


def test_stale_running_reported_but_not_live():
    # A 'running' thread gone quiet beyond the window keeps its raw status but
    # is NOT live — so it can't masquerade as active work (honest, with age).
    reg = CursorAgentRegistry()
    reg._agents["/ws"] = _agent([_sess("oldrun", "running", RUNNING_RECENCY_SEC + 120)])
    s = reg.live_status_for_sid("oldrun")
    assert s is not None
    assert s["status"] == "running"
    assert s["live"] is False
    assert s["age_sec"] >= RUNNING_RECENCY_SEC


def test_live_status_for_sid_full_prefix_and_miss():
    reg = CursorAgentRegistry()
    reg._agents["/ws"] = _agent([_sess("abcdef123456", "running", 4)])
    full = reg.live_status_for_sid("abcdef123456")
    assert full is not None and full["status"] == "running" and full["live"] is True
    pref = reg.live_status_for_sid("abcdef")  # sid prefix (the roster shows sid[:8])
    assert pref is not None and pref["live"] is True
    assert reg.live_status_for_sid("nope-not-a-thread") is None
