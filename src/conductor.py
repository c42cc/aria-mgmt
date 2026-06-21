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

# (input, output) USD per token. Opus 4.8 verified 2026-06-21 ($5/$25 per MTok);
# haiku-4-5 is an approximate fast-tier estimate (cost is for observability, not
# billing — the engine's max_budget is the real cap).
_PRICING = {
    "claude-opus-4-8": (5.0 / 1e6, 25.0 / 1e6),
    "claude-haiku-4-5": (1.0 / 1e6, 5.0 / 1e6),
}
_DEFAULT_PRICE = (5.0 / 1e6, 25.0 / 1e6)

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


def decide(
    transcript: list[dict],
    loops: dict[str, Loop],
    model: str | None = None,
    other_threads: str = "",
) -> ConductorTurn:
    """One conductor turn. `transcript` is the durable conversation as alternating
    user/assistant messages — Aria's real memory, loaded fresh each turn.
    `other_threads`, when set, is raw recent activity from her other threads
    (multi-thread context). `model` overrides the reasoning model (the tiering
    seam — review 2.2). Raises loudly on API/contract failure — a broken conductor
    must never read as a confident empty turn."""
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
    if other_threads.strip():
        system += (
            "\n\n## Recent activity in your OTHER threads (for context; the messages "
            "below are THIS thread)\n" + other_threads
        )
    model_id = model or config.reasoning_model
    resp = _client().messages.create(
        model=model_id,
        max_tokens=config.conductor_max_tokens,
        # Cache the static prompt prefix (persona + loops + facts) so re-sending it
        # with the growing history each turn is cheap (Software 2.0 leans on the
        # model + caching, not a hand-built context manager).
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=[_ARIA_TURN_TOOL],
        tool_choice={"type": "tool", "name": "aria_turn"},
        messages=transcript,
    )
    price_in, price_out = _PRICING.get(model_id, _DEFAULT_PRICE)
    cost = resp.usage.input_tokens * price_in + resp.usage.output_tokens * price_out
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
