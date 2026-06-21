"""Brain + voice-bridge units — the go-gate and the LiveKit text extraction.

No API: conductor.decide is stubbed so the mechanical go-gate is tested in
isolation (the gate must hold regardless of what the model says).
"""

from __future__ import annotations

import pytest

from src import brain as brain_mod
from src import conversation
from src.conductor import ConductorTurn
from src.loops import load_loops


def _turn(phase, slots=None):
    return ConductorTurn(
        phase=phase, speak="…", loop_id="feature-build",
        slots=slots or {}, report_channel="text", cost_usd=0.0,
    )


def test_go_gate_blocks_dispatch_without_confirm(monkeypatch):
    b = brain_mod.AriaBrain(loops=load_loops())
    monkeypatch.setattr(brain_mod.conductor, "decide", lambda *a, **k: _turn("DISPATCH"))
    turn = b.user_turn("go")
    assert b.ready_to_dispatch(turn) is None  # nothing confirmed -> no dispatch


def test_confirm_then_go_passes_gate(monkeypatch):
    b = brain_mod.AriaBrain(loops=load_loops())
    slots = {"repo": "scratch", "change": "x", "acceptance": "y"}
    seq = iter([_turn("CONFIRM", slots), _turn("DISPATCH", slots)])
    monkeypatch.setattr(brain_mod.conductor, "decide", lambda *a, **k: next(seq))
    b.user_turn("build x")
    assert b.pending is not None
    ready = b.ready_to_dispatch(b.user_turn("go"))
    assert ready is not None
    loop, got = ready
    assert loop.id == "feature-build" and got["repo"] == "scratch"
    assert b.pending is None  # consumed


def test_fresh_interview_invalidates_stale_confirm(monkeypatch):
    b = brain_mod.AriaBrain(loops=load_loops())
    seq = iter([_turn("CONFIRM", {"repo": "scratch"}), _turn("INTERVIEW")])
    monkeypatch.setattr(brain_mod.conductor, "decide", lambda *a, **k: next(seq))
    b.user_turn("build x")
    b.user_turn("actually wait")
    assert b.pending is None


def test_tiering_routine_fast_report_opus(monkeypatch):
    from src.config import config

    seen: list[str | None] = []

    def fake_decide(transcript, loops, model=None, **kw):
        seen.append(model)
        return _turn("CONFIRM")

    monkeypatch.setattr(brain_mod.conductor, "decide", fake_decide)
    b = brain_mod.AriaBrain(loops=load_loops())
    b.user_turn("x")     # routine -> fast tier (when tiering on)
    b.report_turn()      # nuanced -> always Opus
    assert seen[-1] == config.reasoning_model
    if config.conductor_tier_routine:
        assert seen[0] == config.fast_model


def test_cancellation_after_confirm_clears_pending(monkeypatch):
    b = brain_mod.AriaBrain(loops=load_loops())
    seq = iter([_turn("CONFIRM", {"repo": "scratch"}), _turn("CHITCHAT")])
    monkeypatch.setattr(brain_mod.conductor, "decide", lambda *a, **k: next(seq))
    b.user_turn("build x")
    assert b.pending is not None
    b.user_turn("forget it")
    assert b.pending is None  # a confirmed plan cannot survive a cancellation


def test_spend_cap_holds_build_gracefully(monkeypatch):
    b = brain_mod.AriaBrain(loops=load_loops())
    monkeypatch.setattr(brain_mod.spend, "at_cap", lambda: True)
    monkeypatch.setattr(brain_mod.outcome_log, "record", lambda **k: None)
    ran = {"v": False}
    monkeypatch.setattr(brain_mod.dispatcher, "run", lambda *a, **k: ran.update(v=True))
    res = b.dispatch(load_loops()["feature-build"], {"repo": "scratch", "change": "x", "acceptance": "y"})
    assert res.delivered is False
    assert not ran["v"]  # never even calls the engine — no detonation
    assert "cap" in (res.broke or "").lower()


def test_brain_feeds_prior_session_history_to_conductor(monkeypatch):
    # a prior session left durable turns on the 'main' thread
    conversation.append(thread="main", session="old", channel="text", role="user", content="my name is Corbin")
    conversation.append(thread="main", session="old", channel="text", role="aria", content="hi Corbin")

    captured: dict = {}

    def fake_decide(messages, loops, model=None, **kw):
        captured["messages"] = messages
        return _turn("CHITCHAT")

    monkeypatch.setattr(brain_mod.conductor, "decide", fake_decide)
    b = brain_mod.AriaBrain(loops=load_loops())  # brand-new session, same 'main' thread
    b.user_turn("what's my name?")
    joined = " ".join(m["content"] for m in captured["messages"])
    assert "Corbin" in joined  # the new session SAW the prior conversation — the fix


def test_voice_latest_user_text():
    pytest.importorskip("livekit.agents")  # the voice extra is optional
    from livekit.agents import llm

    from src.voice import _latest_user_text

    ctx = llm.ChatContext.empty()
    ctx.add_message(role="assistant", content="hi")
    ctx.add_message(role="user", content="the real ask")
    assert _latest_user_text(ctx) == "the real ask"
