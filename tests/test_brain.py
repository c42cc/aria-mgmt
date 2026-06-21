"""Brain + voice-bridge units — the go-gate and the LiveKit text extraction.

No API: conductor.decide is stubbed so the mechanical go-gate is tested in
isolation (the gate must hold regardless of what the model says).
"""

from __future__ import annotations

from src import brain as brain_mod
from src.conductor import ConductorTurn
from src.loops import load_loops
from src.telemetry import Trace


def _turn(phase, slots=None):
    return ConductorTurn(
        phase=phase, speak="…", loop_id="feature-build",
        slots=slots or {}, report_channel="text", cost_usd=0.0,
    )


def test_go_gate_blocks_dispatch_without_confirm(monkeypatch):
    b = brain_mod.AriaBrain(loops=load_loops(), trace=Trace())
    monkeypatch.setattr(brain_mod.conductor, "decide", lambda *a, **k: _turn("DISPATCH"))
    turn = b.user_turn("go")
    assert b.ready_to_dispatch(turn) is None  # nothing confirmed -> no dispatch


def test_confirm_then_go_passes_gate(monkeypatch):
    b = brain_mod.AriaBrain(loops=load_loops(), trace=Trace())
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
    b = brain_mod.AriaBrain(loops=load_loops(), trace=Trace())
    seq = iter([_turn("CONFIRM", {"repo": "scratch"}), _turn("INTERVIEW")])
    monkeypatch.setattr(brain_mod.conductor, "decide", lambda *a, **k: next(seq))
    b.user_turn("build x")
    b.user_turn("actually wait")
    assert b.pending is None


def test_voice_latest_user_text():
    from livekit.agents import llm

    from src.voice import _latest_user_text

    ctx = llm.ChatContext.empty()
    ctx.add_message(role="assistant", content="hi")
    ctx.add_message(role="user", content="the real ask")
    assert _latest_user_text(ctx) == "the real ask"
