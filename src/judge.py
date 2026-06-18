"""Product correctness judge — EMIT/SPEC/JUDGE/SURFACE harness.

Evaluates session records against correctness specs using Gemini Flash.
Product-agnostic: all product knowledge lives in the spec files and session
records, never in this module.

Run via CLI:  python -m src.judge [command]

Governance: this module ADVISES. It never modifies system behavior, overrides
user intent, or intervenes during sessions. See ARCHITECTURE.md Fundamental 13.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .config import config
from .db import get_connection, get_session_record, get_unjudged_records, write_verdict

log = logging.getLogger(__name__)


class JudgeError(Exception):
    """The judge MECHANISM failed (model timeout / HTTP / malformed output).

    A mechanism failure is NOT a verdict. It can never become a silent
    `degraded` score or a dropped `None` — it is raised loud so the caller
    records an `unverified` outcome (which counts as fail) and the gap is
    visible. The live_visuals_4 discipline: a broken instrument must never read
    as a measurement.
    """


def _posture_clause(confirm: bool | None = None) -> str:
    """The approval posture, derived from its ONE home (config.confirm_risky_tools)
    and injected as authoritative so a spec can never independently decide it.

    This collapses the two-home contradiction the post-mortem named: the spec
    (specs/correctness/agent.md) called confirmed=null a violation while config
    configured tier-I/X to run autonomously — the source of `CORRECTNESS FAILED
    score=0.00` on behavior that is configured to be correct.

    `confirm` is read from config when None; the param exists for testing.
    """
    if config.confirm_risky_tools if confirm is None else confirm:
        return (
            "## Runtime Posture (authoritative — overrides any spec text to the contrary)\n"
            "Per-command confirmation is ON (config.confirm_risky_tools=true). A tier-I "
            "(irreversible) or tier-X (executable) tool that fired with confirmed=false or "
            "confirmed=null IS a confirmation violation."
        )
    return (
        "## Runtime Posture (authoritative — overrides any spec text to the contrary)\n"
        "Per-command confirmation is OFF (config.confirm_risky_tools=false): tier-I "
        "(irreversible) and tier-X (executable) MCP tools are CONFIGURED to execute "
        "autonomously and are audited with confirmed=null. Therefore confirmed=null on a "
        "tier-I/X tool is CORRECT and MUST NOT be marked a confirmation violation. Only the "
        "ON posture makes an unconfirmed tier-I/X tool a violation."
    )


SNAPSHOTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "anchor_snapshots"
)

SPECS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "specs", "correctness")
VERDICTS_PATH = os.path.join(config.data_dir, "verdicts.ndjson")

JUDGE_SYSTEM_PROMPT = """\
You are a strict correctness judge. You receive a CORRECTNESS SPEC, a SESSION RECORD \
(including tool trace with every MCP tool call the agent made), and optionally \
ANCHOR GROUND-TRUTH REPORTS from deterministic re-verification of the same data sources.

Your job: determine whether the session outputs satisfy EVERY property in the spec.

## Mandatory procedure

1. COUNT the tool calls in the Tool Trace section. State the exact count. \
Do not estimate or assume — count the ### Call headers.
2. For each spec property, quote the specific evidence from the record that \
confirms or violates it. If you cannot find evidence, say "NOT VERIFIABLE" — \
do not guess or infer.
3. When Anchor Ground-Truth Reports are present, treat anchor facts as \
authoritative. If an anchor says count=501 and the agent claimed count=22, \
the agent is wrong. Do not rationalize the discrepancy.
4. Check every numeric claim in the agent's output against the tool trace. \
If the agent says "13 receipts" but the trace shows 8 matching items, cite \
the mismatch.

## Verdicts

- "correct" — ALL spec properties verified and satisfied. No anchor violations.
- "degraded" — Some properties satisfied, some violated or unverifiable.
- "failed" — Any HARD anchor violation, OR the core task was not accomplished, \
OR the agent fabricated results not grounded in the tool trace.

## Output format

Respond with ONLY valid JSON:
{"verdict": "correct | degraded | failed", "score": 0.0-1.0, "reasons": ["..."]}

score: 1.0 = fully correct, 0.5 = degraded, 0.0 = total failure.
reasons: one string per spec property evaluated. Each MUST cite specific evidence \
(quote tool call numbers, arg values, result excerpts, anchor facts). \
Never write a reason without a citation."""


@dataclass
class Verdict:
    product: str
    session_id: str
    verdict: str
    score: float
    reasons: list[str]
    judged_at: str


def load_spec(product: str) -> str | None:
    """Load a correctness spec file for a product surface."""
    path = os.path.join(SPECS_DIR, f"{product}.md")
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def _serialize_record(record: dict[str, Any]) -> str:
    """Serialize a session record into a text block for the judge."""
    parts = []
    parts.append(f"Tool: {record.get('tool_name', 'unknown')}")
    parts.append(f"Product: {record.get('product', 'unknown')}")
    parts.append(f"Timestamp: {record.get('timestamp', 'unknown')}")

    inputs = record.get("inputs_json")
    if isinstance(inputs, str):
        inputs = json.loads(inputs)
    if inputs:
        parts.append(f"\n## Inputs\n```json\n{json.dumps(inputs, indent=2, default=str)[:1_000_000]}\n```")

    outputs = record.get("outputs_json")
    if isinstance(outputs, str):
        outputs = json.loads(outputs)
    if outputs:
        result = outputs.get("result", "")
        if isinstance(result, str) and len(result) > 1_000_000:
            outputs = {**outputs, "result": result[:1_000_000] + "\n... [truncated]"}
        parts.append(f"\n## Outputs\n```json\n{json.dumps(outputs, indent=2, default=str)[:1_000_000]}\n```")

    context = record.get("context_json")
    if isinstance(context, str) and context:
        context = json.loads(context)
    if context:
        tool_trace = context.get("tool_trace", [])
        if tool_trace:
            parts.append(f"\n## Tool Trace ({len(tool_trace)} tool calls)\n")
            for i, tc in enumerate(tool_trace):
                tool_name = tc.get("tool", "unknown")
                args = tc.get("args", tc.get("args_summary", {}))
                result_chars = tc.get("result_chars", 0)
                truncated = tc.get("result_truncated", False)
                result = tc.get("result", tc.get("result_preview", ""))
                result_preview = result[:1_000_000] if isinstance(result, str) else str(result)[:1_000_000]

                parts.append(f"### Call {i+1}: `{tool_name}`")
                parts.append(f"Args: `{json.dumps(args, default=str)[:100_000]}`")
                parts.append(f"Result size: {result_chars} chars (truncated: {truncated})")
                parts.append(f"Result preview:\n```\n{result_preview}\n```\n")

        other_context = {k: v for k, v in context.items() if k != "tool_trace"}
        if other_context:
            parts.append(f"\n## Other Context\n```json\n{json.dumps(other_context, indent=2, default=str)[:1_000_000]}\n```")

    return "\n".join(parts)


def _write_verdict_ndjson(verdict: Verdict) -> None:
    """Append verdict to data/verdicts.ndjson."""
    os.makedirs(os.path.dirname(VERDICTS_PATH), exist_ok=True)
    entry = {
        "product": verdict.product,
        "session_id": verdict.session_id,
        "timestamp": verdict.judged_at,
        "verdict": verdict.verdict,
        "score": verdict.score,
        "reasons": verdict.reasons,
    }
    try:
        with open(VERDICTS_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        log.warning("Failed to write verdict to NDJSON", exc_info=True)


def _parse_judge_response(text: str) -> dict[str, Any]:
    """Extract JSON from the judge model response, tolerating markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
        raise


async def _run_anchors(record: dict[str, Any]) -> list[dict]:
    """Run deterministic anchors on every tool call in the session trace.

    Routes through `registry.check_with_cache` so concurrent judge runs that
    happen to cover the same `(tool, args)` share a single upstream API
    call. Without this, every parallel agent loop doubled Gmail / Calendar /
    GitHub traffic per anchor (audit gap L6).

    P1 plan extension (F14): if the session was produced by
    `plan_with_claude` and the trace has no entry for it, synthesise a
    virtual entry so the PlanCitationAnchor can inspect the result text.
    Plans are one-shot — they never appear in their own trace otherwise.
    """
    from .anchors import anchor_for
    from .anchors.base import AnchorReport
    from .anchors.registry import check_with_cache

    context = record.get("context_json")
    if isinstance(context, str) and context:
        context = json.loads(context)
    if not isinstance(context, dict):
        context = {}

    tool_trace = list(context.get("tool_trace", []) or [])

    outputs = record.get("outputs_json")
    if isinstance(outputs, str):
        outputs = json.loads(outputs)
    aria_result = (outputs or {}).get("result", "")

    record_tool = record.get("tool_name", "")
    if record_tool == "plan_with_claude" and not any(
        tc.get("tool") == "plan_with_claude" for tc in tool_trace
    ):
        tool_trace.append({
            "tool": "plan_with_claude",
            "args": {},
            "result": aria_result,
            "result_chars": len(aria_result),
            "result_truncated": False,
            "deduped": False,
        })

    if not tool_trace:
        return []

    reports = []
    for tc in tool_trace:
        tool_name = tc.get("tool", "")
        anchor = anchor_for(tool_name)
        if not anchor:
            continue
        try:
            report = await check_with_cache(anchor, tool_name, tc, aria_result)
            reports.append(report.to_dict())
        except Exception:
            log.warning("Anchor check failed for %s", tool_name, exc_info=True)
            reports.append(AnchorReport(tool=tool_name, unverified=True).to_dict())

    return reports


def _format_anchor_section(reports: list[dict]) -> str:
    if not reports:
        return ""
    lines = ["\n## Anchor Ground-Truth Reports\n"]
    lines.append("These are deterministic facts obtained by independently re-querying the ")
    lines.append("source of truth. Use them to verify Aria's claims. If an anchor says ")
    lines.append("a count is X and Aria claimed Y, trust the anchor.\n")
    for r in reports:
        if r.get("unverified"):
            lines.append(f"\n### {r['tool']} — UNVERIFIED (anchor's source was unreachable)\n")
            continue
        lines.append(f"\n### {r['tool']} — anchor verdict: **{r['binary']}**")
        for f in r.get("facts", []):
            val = f["value"]
            if isinstance(val, list) and len(str(val)) > 200:
                val = f"[{len(val)} items]"
            lines.append(f"- {f['key']}: {val} (source: {f['source']})")
        for v in r.get("violations", []):
            lines.append(f"- **VIOLATION** spec#{v['prop']} [{v['severity']}]: {v['detail']}")
    return "\n".join(lines)


def _save_snapshot(session_id: str, reports: list[dict]) -> None:
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOTS_DIR, f"{session_id}.json")
    try:
        with open(path, "w") as f:
            json.dump(reports, f, indent=2, default=str)
    except Exception:
        log.warning("Failed to save anchor snapshot for %s", session_id, exc_info=True)


async def evaluate(
    spec: str, record: dict[str, Any], product: str, session_id: str,
    *, use_anchors: bool = True,
) -> Verdict:
    """Correctness judge with deterministic anchor floor.

    1. Run anchors on every tool call → structured facts + binary verdict.
    2. Compute anchor floor = worst(all anchor binaries).
    3. Run LLM judge with anchor facts injected into the prompt.
    4. Apply floor rule: final = min(anchor_floor, llm_verdict).

    `use_anchors=False` calibrates the LLM judge ALONE over a synthetic corpus:
    anchors re-query LIVE sources (real Gmail/Calendar), so flooring a fabricated
    fixture against live reality is meaningless. The anchor floor is deterministic
    ground-truth for REAL sessions and needs no calibration; what calibration
    earns is trust in the LLM verdict.
    """
    from google import genai
    from .anchors.base import verdict_min

    if use_anchors:
        anchor_reports = await _run_anchors(record)
        _save_snapshot(session_id, anchor_reports)
        # Anchor floor: worst-binary verdict across all verified anchor reports.
        # `None` means we have no verified anchors (every one was unreachable),
        # in which case `evaluate()` falls through to the LLM verdict only.
        verified = [r for r in anchor_reports if not r.get("unverified")]
        if verified:
            from .anchors.base import VERDICT_RANK
            worst = "correct"
            for r in verified:
                b = r.get("binary", "correct")
                if VERDICT_RANK.get(b, 0) < VERDICT_RANK.get(worst, 0):
                    worst = b
            anchor_floor = worst
        else:
            anchor_floor = None
    else:
        anchor_reports = []
        anchor_floor = None

    record_text = _serialize_record(record)
    anchor_section = _format_anchor_section(anchor_reports)

    prompt = (
        f"## Correctness Spec\n\n{spec}\n\n{_posture_clause()}\n\n"
        f"## Session Record\n\n{record_text}{anchor_section}"
    )

    client = genai.Client(api_key=config.google_api_key)
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=JUDGE_SYSTEM_PROMPT,
                temperature=0.0,
            ),
        )
    except Exception as exc:
        # Mechanism failure (timeout / HTTP / SDK) — loud, never a silent verdict.
        raise JudgeError(f"judge model call failed: {type(exc).__name__}: {exc}") from exc

    try:
        parsed = _parse_judge_response(response.text)
        llm_verdict = parsed.get("verdict", "failed")
        if llm_verdict not in ("correct", "degraded", "failed"):
            llm_verdict = "failed"
        score = float(parsed.get("score", 0.0))
        reasons = parsed.get("reasons", [])
        if isinstance(reasons, str):
            reasons = [reasons]
    except (json.JSONDecodeError, ValueError) as exc:
        # Malformed output is a broken instrument, not a 'degraded' measurement.
        raise JudgeError(
            f"judge returned unparseable output: {(response.text or '')[:200]}"
        ) from exc

    if anchor_floor is not None:
        final_verdict = verdict_min(anchor_floor, llm_verdict)
        if final_verdict != llm_verdict:
            reasons.insert(0, f"[ANCHOR FLOOR] LLM judged '{llm_verdict}' but anchor floor is '{anchor_floor}' — capped to '{final_verdict}'")
            score = min(score, {"correct": 1.0, "degraded": 0.5, "failed": 0.0}[final_verdict])
    else:
        final_verdict = llm_verdict

    verdict = Verdict(
        product=product,
        session_id=session_id,
        verdict=final_verdict,
        score=score,
        reasons=reasons,
        judged_at=datetime.now(timezone.utc).isoformat(),
    )

    anchor_reports_json = json.dumps(anchor_reports, default=str) if anchor_reports else None
    write_verdict(
        verdict.product, verdict.session_id, verdict.verdict, verdict.score, verdict.reasons,
        anchor_floor=anchor_floor, anchor_reports_json=anchor_reports_json,
    )
    _write_verdict_ndjson(verdict)

    if verdict.verdict == "failed":
        log.warning("CORRECTNESS FAILED [%s] session=%s reasons=%s", product, session_id, reasons)

    return verdict


async def evaluate_record(record_id: int, product: str) -> Verdict | None:
    """Load a session record by ID and evaluate it. Called by the fire-and-forget task."""
    record = get_session_record(record_id)
    if not record:
        log.warning("Session record %d not found for evaluation", record_id)
        return None

    spec = load_spec(product)
    if not spec:
        log.debug("No correctness spec for product %r, skipping evaluation", product)
        return None

    try:
        return await evaluate(spec, record, product, str(record_id))
    except JudgeError as exc:
        # Mechanism failure: record an `unverified` verdict (counts as fail in the
        # correctness rate) so the gap is visible and never silently dropped to None.
        log.error("Judge MECHANISM failure for record %d: %s", record_id, exc)
        verdict = Verdict(
            product=product,
            session_id=str(record_id),
            verdict="unverified",
            score=0.0,
            reasons=[f"judge mechanism failure: {exc}"],
            judged_at=datetime.now(timezone.utc).isoformat(),
        )
        write_verdict(
            verdict.product, verdict.session_id, verdict.verdict,
            verdict.score, verdict.reasons,
        )
        _write_verdict_ndjson(verdict)
        return verdict


async def sweep_unjudged(
    hours: int = 24,
    alert: Callable[[str], Awaitable[None]] | None = None,
) -> int:
    """Judge every session_record from the last `hours` that has no verdict yet.

    The durable replacement for the fire-and-forget inline judge: an orphaned
    `asyncio.create_task` was dropped on process churn, so ~45% of sessions —
    worst-when-worst: the longest, failed ones — went unjudged. This drains the
    DB worklist and is idempotent via the LEFT JOIN in `get_unjudged_records`
    (a record with a verdict never reappears). One record's failure never
    aborts the rest. Returns the count newly judged.
    """
    records = get_unjudged_records(hours)
    judged = 0
    for rec in records:
        try:
            verdict = await evaluate_record(int(rec["id"]), str(rec["product"]))
        except Exception:
            log.warning("sweep: judge raised for record %s", rec.get("id"), exc_info=True)
            continue
        if verdict is None:
            continue
        judged += 1
        if verdict.verdict in ("failed", "unverified") and alert is not None:
            reasons = "; ".join(verdict.reasons[:3]) if verdict.reasons else "no details"
            label = "Correctness FAILED" if verdict.verdict == "failed" else "Judge UNVERIFIED (mechanism failure)"
            try:
                await alert(
                    f"**{label}** [{verdict.product}] "
                    f"score={verdict.score:.2f}\n{reasons}"
                )
            except Exception:
                log.debug("sweep: failure alert failed (non-fatal)", exc_info=True)
    if judged:
        log.info("judge sweep: judged %d previously-unjudged record(s)", judged)
    return judged


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from .db import init_db, get_correctness_summary, get_recent_verdicts
    init_db()

    args = sys.argv[1:]

    if not args or args[0] == "help":
        print("Usage: python -m src.judge <command>")
        print()
        print("Commands:")
        print("  report [hours]     Correctness summary by product (default 24h)")
        print("  recent [hours]     List recent verdicts (default 24h)")
        print("  specs              List available correctness specs")
        return 0

    cmd = args[0]

    if cmd == "report":
        hours = int(args[1]) if len(args) > 1 else 24
        summary = get_correctness_summary(hours)
        if not summary:
            print(f"No verdicts in the last {hours}h")
            return 0
        print(f"Correctness report (last {hours}h):\n")
        for product, stats in sorted(summary.items()):
            rate = stats["correctness_rate"]
            total = stats["total"]
            print(f"  {product:15s}  {rate:5.0%} correct  ({total} sessions: "
                  f"{stats['correct']} ok, {stats['degraded']} degraded, {stats['failed']} failed)")
        return 0

    if cmd == "recent":
        hours = int(args[1]) if len(args) > 1 else 24
        verdicts = get_recent_verdicts(hours)
        if not verdicts:
            print(f"No verdicts in the last {hours}h")
            return 0
        for v in verdicts:
            reasons = json.loads(v["reasons"]) if v["reasons"] else []
            reason_str = "; ".join(reasons[:2]) if reasons else ""
            print(f"  {v['judged_at'][:19]}  {v['product']:15s}  {v['verdict']:10s}  "
                  f"{v['score']:.2f}  {reason_str[:80]}")
        return 0

    if cmd == "specs":
        if not os.path.isdir(SPECS_DIR):
            print(f"Specs directory not found: {SPECS_DIR}")
            return 1
        for f in sorted(os.listdir(SPECS_DIR)):
            if f.endswith(".md"):
                name = f[:-3]
                path = os.path.join(SPECS_DIR, f)
                with open(path) as fh:
                    lines = fh.readlines()
                first_line = lines[0].strip().lstrip("# ") if lines else "(empty)"
                print(f"  {name:15s}  {first_line}")
        return 0

    print(f"Unknown command: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(_cli_main())
