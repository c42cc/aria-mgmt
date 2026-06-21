"""Aria v2 — the whole loop, legible here.

Read a user turn -> the conductor (Claude) decides what to say and do -> speak it
-> if (and only if) a confirmed plan got an explicit go, dispatch to the engine,
verify against ground truth, and report the honest outcome. Every turn is timed
and traced; every dispatched request writes one outcome-log row.

The mechanical go-gate lives HERE, not in a prompt: nothing dispatches unless a
CONFIRM for this exact loop preceded the user's go. The conductor's judgment is
defense in depth on top of that structural gate.
"""

from __future__ import annotations

import sys
import time

from . import conductor, dispatcher, outcome_log
from .frontends import TextFrontend
from .loops import Loop, load_loops
from .telemetry import Trace


def _decide(transcript: list[dict], loops: dict[str, Loop], trace: Trace, frontend) -> tuple[conductor.ConductorTurn, float]:
    t0 = time.time()
    turn = conductor.decide(transcript, loops)
    latency_ms = int((time.time() - t0) * 1000)
    frontend.say(turn.speak)
    transcript.append({"role": "assistant", "content": turn.speak})
    trace.aria(turn.speak, turn.phase, latency_ms, turn.loop_id)
    return turn, turn.cost_usd


def run_session(frontend, loops: dict[str, Loop]) -> tuple[Trace, float]:
    trace = Trace()
    transcript: list[dict] = []
    pending: tuple[str, dict] | None = None  # (loop_id, slots) awaiting an explicit go
    session_cost = 0.0

    while True:
        user_text = frontend.read()
        if user_text is None:
            break
        user_text = user_text.strip()
        if not user_text:
            continue
        if user_text in {"/quit", "/exit"}:
            break

        transcript.append({"role": "user", "content": user_text})
        trace.user(user_text)

        turn, cost = _decide(transcript, loops, trace, frontend)
        session_cost += cost

        if turn.phase == "CONFIRM":
            pending = (turn.loop_id, turn.slots)
            continue

        if turn.phase != "DISPATCH":
            # CHITCHAT / INTERVIEW / REPORT — nothing to fire.
            if turn.phase == "INTERVIEW":
                pending = None  # a fresh interview invalidates any stale confirm
            continue

        # DISPATCH — enforce the mechanical go-gate.
        if not pending or pending[0] != turn.loop_id:
            obs = "[system] You moved to DISPATCH without a confirmed plan and an explicit go. Confirm first."
            transcript.append({"role": "user", "content": obs})
            trace.observation(obs)
            _decide(transcript, loops, trace, frontend)  # corrective turn
            pending = None
            continue

        loop = loops[turn.loop_id]
        slots = {**(pending[1] or {}), **(turn.slots or {})}
        pending = None

        result = dispatcher.run(loop, slots)
        session_cost += result.cost_usd
        obs = f"[engine result] delivered={result.delivered}. {result.broke or result.summary}"
        transcript.append({"role": "user", "content": obs})
        trace.observation(obs)

        report, cost = _decide(transcript, loops, trace, frontend)
        session_cost += cost

        outcome_log.record(
            request=str(slots.get("change") or slots.get("repo") or loop.id),
            loop_id=loop.id,
            slots=slots,
            delivered=result.delivered,
            summary=result.summary,
            broke=result.broke,
            cost_usd=result.cost_usd,
            extra={"session_id": result.session_id, "tests_passed": result.tests_passed},
        )

    trace.save()
    return trace, session_cost


def main() -> int:
    from . import preflight

    skip_ping = "--no-ping" in sys.argv
    try:
        for line in preflight.check(ping_models=not skip_ping):
            print(f"[preflight] {line}")
    except preflight.PreflightError as e:
        print(f"[preflight] REFUSED: {e}", file=sys.stderr)
        return 1

    loops = load_loops()
    print("Aria v2 — text mode. Type your request; /quit to exit.\n")
    trace, cost = run_session(TextFrontend(), loops)
    print(f"\n[session] {trace.summary()} | est. conductor+engine spend ${cost:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
