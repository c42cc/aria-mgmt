"""AriaBrain — one brain, every transport.

The conductor-driving logic + the mechanical go-gate + dispatch + outcome
logging live HERE, once, so text and voice are two thin transports over the
SAME brain (no second home for the go-gate — operate on the primitive). A
transport feeds a user utterance and gets back the turn(s) to speak; it decides
only the TIMING of a dispatch (text blocks and reports inline; voice speaks a
filler and builds in the background).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import conductor, dispatcher, outcome_log, spend
from .conductor import ConductorTurn
from .config import config
from .dispatcher import DispatchResult
from .loops import Loop
from .telemetry import Trace


@dataclass
class AriaBrain:
    loops: dict[str, Loop]
    trace: Trace = field(default_factory=Trace)
    transcript: list[dict] = field(default_factory=list)
    pending: tuple[str, dict] | None = None  # (loop_id, slots) awaiting an explicit go
    session_cost: float = 0.0

    @property
    def _routine_model(self) -> str:
        # Routine/interview turns: the fast tier (review 2.2). Routing + the guards
        # are verified to hold on it. The nuanced REPORT stays on Opus.
        return config.fast_model if config.conductor_tier_routine else config.reasoning_model

    def _decide(self, model: str | None = None) -> ConductorTurn:
        t0 = time.time()
        turn = conductor.decide(self.transcript, self.loops, model=model)
        latency_ms = int((time.time() - t0) * 1000)
        self.transcript.append({"role": "assistant", "content": turn.speak})
        self.trace.aria(turn.speak, turn.phase, latency_ms, turn.loop_id)
        self.session_cost += turn.cost_usd
        return turn

    def user_turn(self, text: str) -> ConductorTurn:
        """Record the user's utterance and get Aria's next turn (decide + gate)."""
        self.transcript.append({"role": "user", "content": text})
        self.trace.user(text)
        turn = self._decide(self._routine_model)
        # A confirmed plan survives ONLY into the immediately-following DISPATCH.
        # Anything else — a fresh interview, a cancellation ("forget it" -> CHITCHAT),
        # a topic change — clears it, so a later stray "go" can't fire a stale plan.
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
        """Conductor tried to DISPATCH with no confirmed plan — correct it loudly."""
        obs = "[system] You moved to DISPATCH without a confirmed plan and an explicit go. Confirm first."
        self.transcript.append({"role": "user", "content": obs})
        self.trace.observation(obs)
        self.pending = None
        return self._decide(self._routine_model)

    def dispatch(self, loop: Loop, slots: dict) -> DispatchResult:
        """Run the engine, verify against ground truth, log the outcome, and fold
        the result back into the transcript so the next turn can report it."""
        if spend.at_cap():
            broke = (
                f"today's spend cap (${config.daily_spend_cap_usd:.0f}) is reached — "
                "held the build; ask Corbin whether to continue today or pick it up tomorrow"
            )
            obs = f"[engine result] delivered=False. {broke}"
            self.transcript.append({"role": "user", "content": obs})
            self.trace.observation(obs)
            outcome_log.record(
                request=str(slots.get("change") or slots.get("repo") or loop.id),
                loop_id=loop.id, slots=slots, delivered=False, summary=broke,
                broke=broke, cost_usd=0.0, extra={"held": "spend_cap"},
            )
            return DispatchResult(False, broke, broke, "", None, 0.0, "")
        result = dispatcher.run(loop, slots)
        self.session_cost += result.cost_usd
        obs = f"[engine result] delivered={result.delivered}. {result.broke or result.summary}"
        self.transcript.append({"role": "user", "content": obs})
        self.trace.observation(obs)
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
        return result

    def report_turn(self) -> ConductorTurn:
        """After a dispatch, the conductor's honest REPORT line — on Opus, where
        'name the blocker, never fabricate' matters most."""
        return self._decide(config.reasoning_model)
