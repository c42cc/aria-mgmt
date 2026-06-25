"""Fulfillment harness — pure-logic locks (no API).

The live calibration run (real Gemini) is `make fulfillment-calibrate` and the
R5 definition-of-done is `python -m src.fulfillment golden`. These tests lock the
pure parts: the calibration gates (a lenient judge MUST fail; the golden R5
attribution is required; honesty floors fabrication), the refuse-to-trust
freshness check, and the arc-join that turns the durable record into a
context-complete unit.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import db  # noqa: E402
from src import fulfillment as f  # noqa: E402


def _r(id, expected, cls, score, layer="none", golden=False, expected_layer=None):
    # `layer` is what the judge OUTPUT (root_cause_layer); `expected_layer` is what
    # a golden arc must land. A correct golden expects the layer it output; the
    # misattribution test passes a wrong output layer with the right expectation.
    return {
        "id": id, "expected_class": expected,
        "expected_layer": expected_layer if expected_layer is not None else (layer if golden else None),
        "golden": golden, "cls": cls, "root_cause_layer": layer, "score": score,
    }


def _good_corpus_results():
    """A judge that nails the corpus: classes agree, golden is OFF-THE-RAILS +
    dispatch-context, FULFILLED separates above the bad classes, honest blockers
    above fabrication."""
    return [
        _r("r5", f.OFF_THE_RAILS, f.OFF_THE_RAILS, 0.0, "dispatch-context", golden=True),
        _r("r6", f.BLOCKED_UNAVOIDABLE, f.BLOCKED_UNAVOIDABLE, 0.85, "environment"),
        _r("k", f.FULFILLED, f.FULFILLED, 0.97),
        _r("s", f.BLOCKED_AVOIDABLE, f.BLOCKED_AVOIDABLE, 0.30, "engine-reasoning"),
        _r("p", f.PARTIAL, f.PARTIAL, 0.90),
        _r("x", f.FABRICATED, f.FABRICATED, 0.0, "engine-reasoning"),
    ]


def test_perfect_calibration_passes():
    out = f.evaluate_calibration(_good_corpus_results())
    assert out["agreement"] == 1.0
    assert out["golden_ok"] is True
    assert out["separation"] is True
    assert out["honesty"] is True
    assert out["passed"] is True


def test_lenient_judge_fails():
    # The false-green machine: everything is FULFILLED/1.0.
    results = [
        _r("r5", f.OFF_THE_RAILS, f.FULFILLED, 1.0, "none", golden=True),
        _r("r6", f.BLOCKED_UNAVOIDABLE, f.FULFILLED, 1.0),
        _r("k", f.FULFILLED, f.FULFILLED, 1.0),
        _r("x", f.FABRICATED, f.FULFILLED, 1.0),
    ]
    out = f.evaluate_calibration(results)
    assert out["agreement"] < f.AGREEMENT_MIN
    assert out["golden_ok"] is False        # golden wasn't called OFF-THE-RAILS
    assert out["passed"] is False


def test_golden_layer_attribution_required():
    # Everything agrees AND separates, but the golden R5 is blamed on the engine's
    # reasoning instead of the starved dispatch — the exact misattribution this
    # harness exists to refuse. Must fail.
    results = _good_corpus_results()
    results[0] = _r("r5", f.OFF_THE_RAILS, f.OFF_THE_RAILS, 0.0, "engine-reasoning",
                    golden=True, expected_layer="dispatch-context")
    out = f.evaluate_calibration(results)
    assert out["golden_ok"] is False
    assert out["passed"] is False


def test_honesty_floor_required():
    # An honest blocker scored BELOW a fabrication — never allowed.
    results = _good_corpus_results()
    results[1] = _r("r6", f.BLOCKED_UNAVOIDABLE, f.BLOCKED_UNAVOIDABLE, 0.0, "environment")
    results[-1] = _r("x", f.FABRICATED, f.FABRICATED, 0.1, "engine-reasoning")
    out = f.evaluate_calibration(results)
    assert out["honesty"] is False
    assert out["passed"] is False


def test_separation_required():
    # A FULFILLED scored at/under an OFF-THE-RAILS — the scale doesn't separate.
    results = _good_corpus_results()
    results[2] = _r("k", f.FULFILLED, f.FULFILLED, 0.0)
    out = f.evaluate_calibration(results)
    assert out["separation"] is False
    assert out["passed"] is False


def test_is_calibrated_refuses_stale_and_failed(monkeypatch):
    now = datetime(2026, 6, 25, tzinfo=timezone.utc)
    monkeypatch.setattr(f, "latest_calibration",
                        lambda: {"passed": True, "ts": (now - timedelta(days=2)).isoformat()})
    assert f.is_calibrated(now=now) is True
    monkeypatch.setattr(f, "latest_calibration",
                        lambda: {"passed": True, "ts": (now - timedelta(days=99)).isoformat()})
    assert f.is_calibrated(now=now) is False
    monkeypatch.setattr(f, "latest_calibration",
                        lambda: {"passed": False, "ts": now.isoformat()})
    assert f.is_calibrated(now=now) is False
    monkeypatch.setattr(f, "latest_calibration", lambda: None)
    assert f.is_calibrated(now=now) is False


def test_arc_from_corpus_detects_preamble():
    bound = f.arc_from_corpus({
        "asked": "what's the size?",
        "dispatched_task": "Recent conversation thread (most recent last):\n- You: archiving.\nUser just said: what's the size?",
    })
    assert bound.preamble_attached is True
    starved = f.arc_from_corpus({"asked": "Give me the debrief", "dispatched_task": "Give me the debrief"})
    assert starved.preamble_attached is False


# --- the arc-join against the real schema -----------------------------------

def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "state.db"))
    db.init_db()
    return str(tmp_path)


def _insert_record(session_key, task, ts, tool_trace=None, result="ok"):
    import json
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO session_records (session_key, tool_name, product, inputs_json, "
            "outputs_json, context_json, timestamp) VALUES (?,?,?,?,?,?,?)",
            (
                session_key, "do_with_claude", "agent",
                json.dumps({"args": {"task": task, "session_key": session_key}, "transcript": []}),
                json.dumps({"result": result}),
                json.dumps({"tool_trace": tool_trace or []}),
                ts,
            ),
        )


def test_extract_arc_joins_request_to_its_referent(tmp_path, monkeypatch):
    data_dir = _fresh_db(tmp_path, monkeypatch)
    # the referent landed in the cross-channel stream just before the request
    db.append_conversation_turn(
        "cursor_event", "text", "#cursor-watch",
        "[Cursor watch] Cursor task completed in live_visuals_4",
        session_key="", parent_channel="",
    )
    db.append_conversation_turn(
        "user", "text", "#Give me the debrief", "Give me the debrief",
        session_key="thread-r5", parent_channel="room-ucs",
    )
    _insert_record("thread-r5", "Give me the debrief", datetime.now(timezone.utc).isoformat())

    arcs = f.extract_arcs(data_dir=data_dir, hours=72)
    assert len(arcs) == 1
    arc = arcs[0]
    assert arc.asked == "Give me the debrief"
    assert arc.dispatched_task == "Give me the debrief"
    assert arc.preamble_attached is False                 # context-starved
    assert arc.user_turn_matched is True
    # the referent is present in the antecedent window the judge will resolve against
    assert any("live_visuals_4" in t["text"] for t in arc.antecedent)
