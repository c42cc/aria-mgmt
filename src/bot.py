"""Aria v2 — the text transport over AriaBrain. Legible end to end.

Read a user turn -> AriaBrain decides + speaks -> if a confirmed plan got an
explicit go, dispatch (text blocks and reports inline). Voice (src/voice.py) is
the same brain with a different dispatch timing.
"""

from __future__ import annotations

import sys

from .brain import AriaBrain
from .frontends import TextFrontend
from .loops import Loop, load_loops
from .telemetry import Trace


def run_session(frontend, loops: dict[str, Loop]) -> tuple[Trace, float]:
    brain = AriaBrain(loops=loops, trace=Trace())

    while True:
        user_text = frontend.read()
        if user_text is None:
            break
        user_text = user_text.strip()
        if not user_text:
            continue
        if user_text in {"/quit", "/exit"}:
            break

        turn = brain.user_turn(user_text)
        frontend.say(turn.speak)

        ready = brain.ready_to_dispatch(turn)
        if turn.phase == "DISPATCH" and ready is None:
            frontend.say(brain.dispatch_violation().speak)
            continue
        if ready:
            loop, slots = ready
            brain.dispatch(loop, slots)  # text blocks on the build
            frontend.say(brain.report_turn().speak)

    brain.trace.save()
    return brain.trace, brain.session_cost


def main() -> int:
    from . import preflight

    skip_ping = "--no-ping" in sys.argv
    try:
        for line in preflight.check(ping_models=not skip_ping):
            print(f"[preflight] {line}")
    except preflight.PreflightError as e:
        print(f"[preflight] REFUSED: {e}", file=sys.stderr)
        return 1

    print("Aria v2 — text mode. Type your request; /quit to exit.\n")
    trace, cost = run_session(TextFrontend(), load_loops())
    print(f"\n[session] {trace.summary()} | est. spend ${cost:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
