"""The dispatch-boundary verified-done floor — the producer-side half of
"done means verified".

Every state-changing (W/I/X) tool call, at the moment it fires, has its
registered post-condition anchor re-consult the source of truth:

  - a lag-free HARD failure (the artifact is provably absent — the file does
    not exist, the created event id does not resolve) is a loud BLOCK, so the
    agent loop can never narrate a success that did not happen;
  - a check that cannot run (the verifier is unreachable), a lag-prone source
    (a just-sent message not yet indexed in Sent), or a soft/degraded violation
    is a loud UNCONFIRMED annotation — never a silent pass, never a false wall.

This is the recombination the forensic audit named. The deterministic anchor
floor used to run ONLY in the async correctness judge (`src/judge.py`, a
catcher minutes later via the 120s sweep); it now runs at the producer —
`src/mcp.py::MCPClient.call_tool` — where the action happens. The judge keeps
the LLM-semantic, narration-faithfulness half (it needs Aria's final words);
the deterministic "did the write land?" half lives here, at the source.

Pure of any dependency on `src.mcp` (which imports THIS module): the dispatcher
formats the typed error from the verdict; this module only decides.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from .base import AnchorReport
from .registry import anchor_for

log = logging.getLogger(__name__)

# Tiers whose calls change the world and therefore must prove they happened.
STATE_CHANGING_TIERS = frozenset({"W", "I", "X"})

# A post-condition re-query must never itself hang the action. Bounded; a
# timeout is an honest UNCONFIRMED, never a silent pass and never an infinite
# wait that swallows the action.
_POSTCOND_TIMEOUT_SEC = 12.0

PASS = "pass"
BLOCK = "block"
ANNOTATE = "annotate"


@dataclass(frozen=True)
class PostconditionVerdict:
    """What the dispatcher should do with a state-changing result.

    decision:
      PASS     — no anchor, a non-state-changing tier, or the artifact is
                 confirmed present. Return the result unchanged.
      BLOCK    — a lag-free anchor proved the artifact ABSENT. The dispatcher
                 returns a typed error so the loop BLOCKS (never reports done).
      ANNOTATE — the artifact could not be positively confirmed. The dispatcher
                 appends `annotation` to the result so Aria narrates the caveat.
    """

    decision: str
    binary: str = "correct"      # the anchor binary, or "unverified"
    message: str = ""            # BLOCK: the one-line user-facing failure
    detail: str = ""             # BLOCK/ANNOTATE: raw detail for the typed error / audit
    annotation: str = ""         # ANNOTATE: the visible note appended to the result
    tool: str = ""

    @property
    def audit_summary(self) -> str:
        return json.dumps({
            "postcondition": self.decision,
            "binary": self.binary,
            "detail": self.detail[:500],
        })


async def evaluate(
    tool_name: str, tier: str, args: dict, result_text: str
) -> PostconditionVerdict:
    """Run the registered post-condition for a state-changing tool result.

    Calls `anchor.check` DIRECTLY (not the read-coalescing cache): every write
    is a unique side effect and must be verified fresh. `aria_result` is "" —
    a post-condition checks the artifact from {args, result}, not Aria's
    narration (that is the judge's job).
    """
    if tier not in STATE_CHANGING_TIERS:
        return PostconditionVerdict(PASS, tool=tool_name)

    anchor = anchor_for(tool_name)
    if anchor is None:
        # Coverage is enforced mechanically by tools/structural_absence_check.py
        # (every W/I/X verb must have a post-condition or a declared waiver), so
        # a missing anchor is tracked, visible debt — not a silent hole here.
        return PostconditionVerdict(PASS, tool=tool_name)

    tc = {
        "tool": tool_name,
        "args": args,
        "result": result_text,
        "result_chars": len(result_text or ""),
        "result_truncated": False,
    }
    try:
        report = await asyncio.wait_for(
            anchor.check(tc, ""), timeout=_POSTCOND_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        return _annotate(
            tool_name, "unverified",
            "the verification did not complete in time",
            "post-condition check timed out",
        )
    except Exception as exc:
        # A broken verifier must read as UNCONFIRMED, never as a pass and never
        # as a crash that takes the whole tool call down.
        log.warning("post-condition anchor raised for %s", tool_name, exc_info=True)
        return _annotate(
            tool_name, "unverified",
            "the verification could not run",
            f"post-condition check error: {type(exc).__name__}: {exc}",
        )

    if report.unverified:
        reason = _first_status(report) or "the source of truth was unreachable"
        return _annotate(tool_name, "unverified", reason, reason)

    if report.binary == "failed":
        detail = _first_violation(report) or "the expected artifact was not found"
        if bool(getattr(anchor, "immediate", True)):
            return PostconditionVerdict(
                BLOCK, binary="failed", tool=tool_name, detail=detail,
                message=(
                    f"the {tool_name} post-condition FAILED — {detail}. The change "
                    "did not land; I did not report it as done."
                ),
            )
        # Lag-prone source (e.g. a just-sent message not yet indexed in Sent):
        # loud, but not a false wall. The async correctness judge re-checks once
        # the source settles.
        return _annotate(
            tool_name, "failed",
            f"could not yet confirm it landed ({detail})", detail,
        )

    if report.binary == "degraded":
        detail = _first_violation(report) or "a soft post-condition check did not pass"
        return _annotate(tool_name, "degraded", detail, detail)

    return PostconditionVerdict(PASS, binary="correct", tool=tool_name)


def _annotate(tool_name: str, binary: str, reason: str, detail: str) -> PostconditionVerdict:
    note = (
        f"[POST-CONDITION UNCONFIRMED] The {tool_name} side effect could not be "
        f"verified: {reason}. Do NOT claim it succeeded as fact — say you attempted "
        f"it and could not confirm it landed."
    )
    return PostconditionVerdict(
        ANNOTATE, binary=binary, tool=tool_name, detail=detail, annotation=note
    )


def _first_violation(report: AnchorReport) -> str:
    for v in report.violations:
        return v.detail
    return ""


def _first_status(report: AnchorReport) -> str:
    for f in report.facts:
        if f.key in ("error", "status"):
            return str(f.value)
    return ""
