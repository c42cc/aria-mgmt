"""AriaBrain — one brain, every transport, over the DURABLE conversation.

The conductor-driving logic + the mechanical go-gate + dispatch live here, once.
The conversation itself is NOT held in RAM — it lives in src/conversation.py, so
Aria's context survives the session: every turn she loads the recent history of
this thread (plus a glance at her other threads) and the model sees it. That is
the fix for "she never has the right context": the transcript is durable and
fed back in as data each turn (Software 2.0), not a per-process scratchpad.

A transport feeds a user utterance and gets back the turn(s) to speak; it decides
only the TIMING of a dispatch (text blocks and reports inline; voice speaks a
filler and builds in the background).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from . import conductor, conversation, dispatcher, outcome_log, spend
from .conductor import ConductorTurn
from .config import config
from .dispatcher import DispatchResult
from .loops import Loop


def _new_session() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


@dataclass
class AriaBrain:
    loops: dict[str, Loop]
    thread: str = field(default_factory=lambda: config.default_thread)
    session: str = field(default_factory=_new_session)
    channel: str = "text"
    pending: tuple[str, dict] | None = None
    session_cost: float = 0.0

    @property
    def _routine_model(self) -> str:
        return config.fast_model if config.conductor_tier_routine else config.reasoning_model

    def _append(self, role: str, content: str, **metrics) -> None:
        conversation.append(
            thread=self.thread, session=self.session, channel=self.channel,
            role=role, content=content, **metrics,
        )

    def _decide(self, model: str | None = None) -> ConductorTurn:
        messages = conversation.thread_messages(self.thread)
        others = conversation.other_threads_context(self.thread)
        t0 = time.time()
        turn = conductor.decide(messages, self.loops, model=model, other_threads=others)
        latency_ms = int((time.time() - t0) * 1000)
        self._append(
            "aria", turn.speak, phase=turn.phase, latency_ms=latency_ms,
            loop_id=turn.loop_id, cost_usd=turn.cost_usd,
        )
        self.session_cost += turn.cost_usd
        return turn

    def user_turn(self, text: str) -> ConductorTurn:
        """Record the user's utterance and get Aria's next turn (decide + gate)."""
        self._append("user", text)
        turn = self._decide(self._routine_model)
        # A confirmed plan survives ONLY into the immediately-following DISPATCH.
        if turn.phase == "CONFIRM":
            self.pending = (turn.loop_id, turn.slots)
        elif turn.phase in ("INTERVIEW", "CHITCHAT", "REPORT"):
            self.pending = None
        return turn

    def ready_to_dispatch(self, turn: ConductorTurn) -> tuple[Loop, dict] | None:
        """The mechanical go-gate: a DISPATCH only fires if a CONFIRM for this
        exact loop preceded the user's go. Returns (loop, slots) or None."""
        if turn.phase != "DISPATCH":
            return None
        if not self.pending or self.pending[0] != turn.loop_id:
            return None
        loop = self.loops[turn.loop_id]
        slots = {**(self.pending[1] or {}), **(turn.slots or {})}
        self.pending = None
        return loop, slots

    def dispatch_violation(self) -> ConductorTurn:
        self._append("observation", "[system] You moved to DISPATCH without a confirmed plan and an explicit go. Confirm first.")
        self.pending = None
        return self._decide(self._routine_model)

    def dispatch(self, loop: Loop, slots: dict) -> DispatchResult:
        """Run the engine, verify against ground truth, log the outcome, and fold
        the result back into the conversation so the next turn can report it."""
        if spend.at_cap():
            broke = (
                f"today's spend cap (${config.daily_spend_cap_usd:.0f}) is reached — "
                "held the build; ask Corbin whether to continue today or pick it up tomorrow"
            )
            self._append("observation", f"[engine result] delivered=False. {broke}")
            outcome_log.record(
                request=str(slots.get("change") or slots.get("repo") or loop.id),
                loop_id=loop.id, slots=slots, delivered=False, summary=broke,
                broke=broke, cost_usd=0.0, extra={"held": "spend_cap"},
            )
            return DispatchResult(False, broke, broke, "", None, 0.0, "")

        result = dispatcher.run(loop, slots)
        self.session_cost += result.cost_usd
        self._append("observation", f"[engine result] delivered={result.delivered}. {result.broke or result.summary}")
        outcome_log.record(
            request=str(slots.get("change") or slots.get("repo") or loop.id),
            loop_id=loop.id, slots=slots, delivered=result.delivered, summary=result.summary,
            broke=result.broke, cost_usd=result.cost_usd,
            extra={"session_id": result.session_id, "tests_passed": result.tests_passed},
        )
        return result

    def report_turn(self) -> ConductorTurn:
        """After a dispatch, the conductor's honest REPORT line — on Opus, where
        'name the blocker, never fabricate' matters most."""
        return self._decide(config.reasoning_model)
