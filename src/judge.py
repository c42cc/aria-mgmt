"""Product correctness judge — EMIT/SPEC/JUDGE/SURFACE harness.

Evaluates session records against correctness specs using Gemini Flash.
Product-agnostic: all product knowledge lives in the spec files and session
records, never in this module.

Run via CLI:  python -m src.judge [command]

Governance: this module ADVISES. It never modifies system behavior, overrides
user intent, or intervenes during sessions. See _ARCHITECTURE.md Fundamental 13.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .config import config
from .db import get_connection, get_session_record, write_verdict

log = logging.getLogger(__name__)

SPECS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "specs", "correctness")
VERDICTS_PATH = os.path.join(config.data_dir, "verdicts.ndjson")

JUDGE_SYSTEM_PROMPT = """\
You are a correctness judge. You receive a CORRECTNESS SPEC and a SESSION RECORD.

Your job: determine whether the session outputs satisfy the spec given the inputs.

Rules:
- Judge ONLY the properties declared in the spec. Do not invent criteria.
- Be objective. If a property cannot be verified from the record, note it but do not penalize.
- "degraded" means partially correct — some properties hold, others violated.
- "failed" means the output fundamentally does not satisfy the spec.
- "correct" means all verifiable spec properties are satisfied.

Respond with ONLY valid JSON matching this schema:
{"verdict": "correct | degraded | failed", "score": 0.0-1.0, "reasons": ["..."]}

score guide: 1.0 = fully correct, 0.5 = degraded with significant issues, 0.0 = total failure.
reasons: list specific violations or confirmations. Be concrete, cite evidence from the record."""


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
        parts.append(f"\n## Inputs\n```json\n{json.dumps(inputs, indent=2, default=str)[:6000]}\n```")

    outputs = record.get("outputs_json")
    if isinstance(outputs, str):
        outputs = json.loads(outputs)
    if outputs:
        result = outputs.get("result", "")
        if isinstance(result, str) and len(result) > 4000:
            outputs = {**outputs, "result": result[:4000] + "\n... [truncated]"}
        parts.append(f"\n## Outputs\n```json\n{json.dumps(outputs, indent=2, default=str)[:6000]}\n```")

    context = record.get("context_json")
    if isinstance(context, str) and context:
        context = json.loads(context)
    if context:
        parts.append(f"\n## Context\n```json\n{json.dumps(context, indent=2, default=str)[:2000]}\n```")

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


async def evaluate(spec: str, record: dict[str, Any], product: str, session_id: str) -> Verdict:
    """Product-agnostic correctness judge. Core harness function."""
    from google import genai

    record_text = _serialize_record(record)

    prompt = f"## Correctness Spec\n\n{spec}\n\n## Session Record\n\n{record_text}"

    client = genai.Client(api_key=config.google_api_key)
    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            system_instruction=JUDGE_SYSTEM_PROMPT,
            temperature=0.0,
        ),
    )

    try:
        parsed = _parse_judge_response(response.text)
        verdict_str = parsed.get("verdict", "failed")
        if verdict_str not in ("correct", "degraded", "failed"):
            verdict_str = "failed"
        score = float(parsed.get("score", 0.0))
        reasons = parsed.get("reasons", [])
        if isinstance(reasons, str):
            reasons = [reasons]
    except (json.JSONDecodeError, ValueError):
        log.warning("Judge returned unparseable response: %s", response.text[:200])
        verdict_str = "degraded"
        score = 0.5
        reasons = [f"Judge response unparseable: {response.text[:200]}"]

    verdict = Verdict(
        product=product,
        session_id=session_id,
        verdict=verdict_str,
        score=score,
        reasons=reasons,
        judged_at=datetime.now(timezone.utc).isoformat(),
    )

    write_verdict(verdict.product, verdict.session_id, verdict.verdict, verdict.score, verdict.reasons)
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
    except Exception:
        log.warning("Judge evaluation failed for record %d", record_id, exc_info=True)
        return None


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
