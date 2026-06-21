"""Aria v2 — the text transport over AriaBrain. Legible end to end.

Read a user turn -> AriaBrain decides + speaks (over the durable conversation) ->
if a confirmed plan got an explicit go, dispatch (text blocks and reports
inline). Voice (src/voice.py) is the same brain with a different dispatch timing.
Context is durable: a new session continues the prior conversation.
"""

from __future__ import annotations

import sys

from . import conversation
from .brain import AriaBrain
from .config import config
from .frontends import TextFrontend
from .loops import Loop, load_loops


def run_session(frontend, loops: dict[str, Loop], thread: str | None = None) -> AriaBrain:
    brain = AriaBrain(loops=loops, thread=thread or config.default_thread)

    while True:
        user_text = frontend.read()
        if user_text is None:
            break
        user_text = user_text.strip()
        if not user_text:
            continue
        if user_text in {"/quit", "/exit"}:
            break
        if user_text.startswith("/thread"):
            parts = user_text.split(maxsplit=1)
            brain.thread = parts[1].strip() if len(parts) > 1 else config.default_thread
            frontend.say(f"(on thread '{brain.thread}')")
            continue

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

    return brain


def main() -> int:
    from . import preflight

    skip_ping = "--no-ping" in sys.argv
    try:
        for line in preflight.check(ping_models=not skip_ping):
            print(f"[preflight] {line}")
    except preflight.PreflightError as e:
        print(f"[preflight] REFUSED: {e}", file=sys.stderr)
        return 1

    print("Aria v2 — text mode. '/thread <name>' to switch threads, '/quit' to exit.")
    print("(She remembers across sessions — this continues your last conversation.)\n")
    brain = run_session(TextFrontend(), load_loops())
    lat = sorted(conversation.latencies())
    p50 = lat[len(lat) // 2] if lat else None
    print(f"\n[session {brain.session}] thread={brain.thread} | spend ${brain.session_cost:.4f} | latency p50={p50}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
