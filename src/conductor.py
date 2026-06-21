"""The conductor — Claude owns the conversation's content.

This is the fix for the defect that sank v1 (review 1.1): the hardest step —
turning a vague request into the right intent, the right loop, and the right
questions — is done by Claude, not by a fast non-reasoning front door. One Claude
call per user turn returns a single structured decision (a phase + what to say),
via forced tool use so the structure is reliable. The fast voice layer (Phase 1)
only renders `speak`; it never decides.

One generic interpreter serves every loop. Adding a capability is adding a loop
file, never touching this code.
"""

from __future__ import annotations

from dataclasses import dataclass

import anthropic

from . import memory, projects, prompts
from .config import config
from .loops import Loop

# Opus 4.8 pricing (verified 2026-06-21): $5 / $25 per MTok in/out.
_IN_PER_TOK = 5.0 / 1_000_000
_OUT_PER_TOK = 25.0 / 1_000_000

PHASES = ("CHITCHAT", "INTERVIEW", "CONFIRM", "DISPATCH", "REPORT")

_ARIA_TURN_TOOL = {
    "name": "aria_turn",
    "description": "Aria's single decision for this turn of the conversation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "phase": {"type": "string", "enum": list(PHASES)},
            "speak": {"type": "string", "description": "Aria's next utterance — voice-shaped: one or two short sentences, no markdown."},
            "loop_id": {"type": "string", "description": "The loop you've identified, once you have one."},
            "slots": {"type": "object", "description": "Slot key -> value filled so far (include pre-filled-from-memory)."},
            "report_channel": {"type": "string", "description": "Where the result goes (e.g. text)."},
        },
        "required": ["phase", "speak"],
    },
}


@dataclass
class ConductorTurn:
    phase: str
    speak: str
    loop_id: str | None
    slots: dict
    report_channel: str | None
    cost_usd: float


def _render_loops(loops: dict[str, Loop]) -> str:
    out = []
    for lp in loops.values():
        qs = "; ".join(f"{s.key} ({s.ask})" for s in lp.required_slots)
        opt = "; ".join(f"{s.key} ({s.ask})" for s in lp.optional_slots)
        out.append(
            f"- id: {lp.id}\n  name: {lp.name}\n  for: {lp.description.strip()}\n"
            f"  declares: {lp.loop}\n  required: {qs}\n  optional: {opt or '(none)'}\n"
            f"  reports to: {lp.report}"
        )
    return "\n".join(out)


def _client() -> anthropic.Anthropic:
    if not config.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set — the conductor cannot think. Fix .env.")
    return anthropic.Anthropic(api_key=config.anthropic_api_key, timeout=config.anthropic_timeout_sec)


def decide(transcript: list[dict], loops: dict[str, Loop]) -> ConductorTurn:
    """One conductor turn. `transcript` is alternating user/assistant messages
    (observations are injected as user turns). Raises loudly on API/contract
    failure — a broken conductor must never read as a confident empty turn."""
    known = ", ".join(sorted(projects.registry())) or "(none registered)"
    system = (
        prompts.load("conductor")
        + "\n\n## The loop library (your capability surface)\n"
        + _render_loops(loops)
        + "\n\n## Known projects (a repo must be one of these, or an absolute path)\n"
        + known
        + "\n\n## Durable facts known about Corbin (pre-fill slots from these)\n"
        + memory.render_for_prompt()
    )
    resp = _client().messages.create(
        model=config.reasoning_model,
        max_tokens=config.conductor_max_tokens,
        system=system,
        tools=[_ARIA_TURN_TOOL],
        tool_choice={"type": "tool", "name": "aria_turn"},
        messages=transcript,
    )
    cost = resp.usage.input_tokens * _IN_PER_TOK + resp.usage.output_tokens * _OUT_PER_TOK
    block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    if block is None:
        raise RuntimeError(f"conductor returned no aria_turn tool call (stop_reason={resp.stop_reason})")
    inp = block.input or {}
    phase = inp.get("phase")
    if phase not in PHASES:
        raise RuntimeError(f"conductor returned invalid phase {phase!r}")
    return ConductorTurn(
        phase=phase,
        speak=(inp.get("speak") or "").strip(),
        loop_id=inp.get("loop_id") or None,
        slots=inp.get("slots") or {},
        report_channel=inp.get("report_channel"),
        cost_usd=cost,
    )
