"""src.fulfillment — request-fulfillment evaluation (intent-first, arc-complete).

Advisory. NEVER gates Aria's "done"; it measures whether she served the user's
TRUE intent and turns the findings into named fixes.

It exists because the shipped correctness judge — and the first draft of this
harness — judged the *literal* request and the *surface* action, called a
confabulated tangent "relevant," and missed a catastrophic intent-misread (R5,
"Give me the debrief"). The fix is structural: the unit of evaluation is the
**arc** (the context-complete request), and judging is **two stages** —
reconstruct the true intent from the arc FIRST, then score against it.

One primitive, two consumers. `antecedent_window()` resolves a request's
referent from the cross-channel stream. This harness uses it to JUDGE in
context; the dispatch (`src/conversation.py::as_claude_context`) uses the same
rule to BIND the antecedent so the engine isn't starved in the first place — the
producer of R5 and the catcher of R5 share one home (see ABSENCES.md / wiring).

Reuses the judge spine: `judge._parse_judge_response` + `judge.JudgeError`, and
`build_hash` for the refuse-to-trust calibration receipt. `use_anchors` has no
analog here — fulfillment is judged from the arc, not re-queried live.

CLI:  python -m src.fulfillment {report|calibrate|golden|show <id>}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import build_hash as _bh
from .config import config
from .judge import JudgeError, _parse_judge_response

log = logging.getLogger(__name__)

# --- the shared antecedent rule (one home for "what could 'the X' point at") ---
# A referent ("the debrief", "this", "the results") can point at a cross-channel
# event that landed shortly before the request. The window bounds how far back;
# the cursor-watch cap mirrors conversation._MAX_CURSOR_EVENTS_IN_CLAUDE_CONTEXT
# so the harness checks for exactly the antecedent the dispatch fix binds.
ANTECEDENT_WINDOW_SEC = 15 * 60
MAX_ANTECEDENT_TURNS = 14
# The marker `conversation.as_claude_context` emits when it DID attach context.
# Its presence in the dispatched task is structural proof the engine was fed the
# room's antecedent; its absence (with a non-empty window) is context-starvation.
PREAMBLE_MARKER = "Recent conversation thread"

FULFILLED = "FULFILLED"
PARTIAL = "PARTIAL"
OFF_THE_RAILS = "OFF-THE-RAILS"
BLOCKED_AVOIDABLE = "BLOCKED-AVOIDABLE"
BLOCKED_UNAVOIDABLE = "BLOCKED-UNAVOIDABLE"
FABRICATED = "FABRICATED"
CLASSES = (
    FULFILLED, PARTIAL, OFF_THE_RAILS,
    BLOCKED_AVOIDABLE, BLOCKED_UNAVOIDABLE, FABRICATED,
)
BLOCKED_CLASSES = (BLOCKED_AVOIDABLE, BLOCKED_UNAVOIDABLE)
LAYERS = ("dispatch-context", "engine-reasoning", "environment", "permission", "capability-gap", "none")

# The capability classes Aria actually holds, so `effectiveness` penalizes only
# paths she HAD and did not try (a wall is a hypothesis, not a stop). One home;
# real arcs inherit it, corpus fixtures may override per-entry.
DEFAULT_CORPUS_OF_ACCESS = [
    "email (search_emails, send_email)",
    "calendar (calendar_events) — may be gated by a macOS Automation grant",
    "model backup to GCS (backup_model) — needs gcloud auth",
    "Nvidia Spark nodes over Tailscale SSH (spark_status, spark_run, ssh spark1/spark2)",
    "Cursor IDE drive + cursor-watch event stream",
    "GitHub, filesystem, 42c.pw account provisioning",
    "Discord post/read across channels",
]

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC_PATH = os.path.join(_REPO, "specs", "correctness", "fulfillment.md")
_CORPUS = os.path.join(_REPO, "evals", "fulfillment_corpus.json")
_RECEIPTS_DIR = os.path.join(config.data_dir, "receipts")

AGREEMENT_MIN = 0.9
CALIBRATION_VALIDITY_DAYS = 14


# ---------------------------------------------------------------------------
# Data structures — the arc and the verdict
# ---------------------------------------------------------------------------

@dataclass
class Arc:
    """The context-complete unit of evaluation. A request is NEVER judged
    without its referent, so every field needed to understand AND score it
    lives here."""
    request_id: str
    session_key: str
    ts: str
    asked: str                     # the user's verbatim request
    dispatched_task: str           # the exact task string the engine received
    transcript: list[dict[str, str]]   # inputs_json.transcript (voice path)
    antecedent: list[dict[str, str]]   # cross-channel turns before the request
    corpus_of_access: list[str]
    tool_trace: list[dict[str, Any]]
    response: str
    preamble_attached: bool        # did the dispatch carry the room's antecedent?
    user_turn_matched: bool        # did we resolve the raw user turn (vs. fall back)?
    # What was around her where she engaged (the awareness surface): the
    # conversational surface (recent shared files), her recent artifacts, watched
    # work. A referent to "what's around" ("the panther video") resolves here.
    surroundings: list[dict[str, str]] = field(default_factory=list)


@dataclass
class FulfillmentVerdict:
    request_id: str
    asked: str
    true_intent: str
    referent: str
    could_resolve: bool
    cls: str
    root_cause_layer: str
    intent_match: float
    completeness: float
    effectiveness: float
    concision: float
    score: float
    the_one_fix: str
    reasons: list[str]
    preamble_attached: bool
    judged_at: str
    arc: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Arc extraction — assemble context-complete units from the durable record
# ---------------------------------------------------------------------------

def _state_db(data_dir: str | None) -> str:
    return os.path.join(data_dir or config.data_dir, "state.db")


def _connect(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"state.db not found at {path} — point --data-dir at the live Aria "
            f"data directory (ucs2-notify-on-stop/data)."
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def antecedent_window(
    conn: sqlite3.Connection,
    request_ts: str,
    parent_channel: str,
    session_key: str,
) -> list[dict[str, str]]:
    """The bounded set of cross-channel turns a request could be referring to:
    the room's recent user/aria exchange PLUS the most-recent cursor-watch /
    alert events — the "what just happened" the user is plausibly pointing at.

    This is the SAME selection the dispatch fix binds into the engine context.
    Here it is the evidence the judge resolves the referent against.
    """
    try:
        cutoff = (
            datetime.fromisoformat(request_ts) - timedelta(seconds=ANTECEDENT_WINDOW_SEC)
        ).isoformat()
    except ValueError:
        cutoff = ""
    rows = conn.execute(
        "SELECT ts, role, channel, session_key, parent_channel, text "
        "FROM conversation_log WHERE ts < ? AND ts >= ? ORDER BY id",
        (request_ts, cutoff),
    ).fetchall()

    kept: list[dict[str, str]] = []
    for r in rows:
        role = r["role"]
        # The room's own timeline (how "that"/"the plan" resolve) + the ambient
        # cursor-watch/alert stream (how "the debrief" resolves). Other rooms'
        # user/aria turns stay out — they are not this request's context.
        if role in ("cursor_event", "alert"):
            keep = True
        elif role in ("user", "aria"):
            keep = bool(parent_channel and r["parent_channel"] == parent_channel) \
                or r["session_key"] == session_key
        else:
            keep = False
        if keep:
            kept.append({
                "ts": r["ts"], "role": role, "channel": r["channel"] or "",
                "text": (r["text"] or "").strip(),
            })
    # Most-recent-last, capped so the firehose can't drown the request.
    return kept[-MAX_ANTECEDENT_TURNS:]


def _match_user_turn(
    conn: sqlite3.Connection, session_key: str, before_ts: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT ts, text, parent_channel FROM conversation_log "
        "WHERE session_key = ? AND role = 'user' AND ts <= ? ORDER BY id DESC LIMIT 1",
        (session_key, before_ts),
    ).fetchone()


def extract_arcs(
    data_dir: str | None = None, hours: int = 72, limit: int | None = None
) -> list[Arc]:
    """Assemble one arc per `do_with_claude` session record in the window."""
    conn = _connect(_state_db(data_dir))
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            "SELECT id, session_key, inputs_json, outputs_json, context_json, timestamp "
            "FROM session_records WHERE tool_name = 'do_with_claude' AND timestamp >= ? "
            "ORDER BY id DESC",
            (cutoff,),
        ).fetchall()
        if limit:
            rows = rows[:limit]
        arcs = []
        for row in rows:
            arcs.append(_arc_from_record(conn, dict(row)))
        return arcs
    finally:
        conn.close()


def _arc_from_record(conn: sqlite3.Connection, rec: dict[str, Any]) -> Arc:
    inputs = _parse_json(rec.get("inputs_json"), {})
    outputs = _parse_json(rec.get("outputs_json"), {})
    context = _parse_json(rec.get("context_json"), {}) or {}
    args = inputs.get("args", {}) if isinstance(inputs, dict) else {}
    dispatched_task = str(args.get("task", "")) if isinstance(args, dict) else ""
    session_key = str(rec.get("session_key") or args.get("session_key", ""))
    record_ts = str(rec.get("timestamp") or "")

    user_turn = _match_user_turn(conn, session_key, record_ts) if session_key else None
    if user_turn is not None:
        asked = (user_turn["text"] or "").strip()
        request_ts = user_turn["ts"]
        parent_channel = user_turn["parent_channel"] or ""
        matched = True
    else:
        # No fall-back fiction: if there is no user turn we judge the dispatched
        # task as the ask, and say so honestly (user_turn_matched=False).
        asked = dispatched_task
        request_ts = record_ts
        parent_channel = ""
        matched = False

    antecedent = antecedent_window(conn, request_ts, parent_channel, session_key)
    return Arc(
        request_id=str(rec.get("id")),
        session_key=session_key,
        ts=request_ts,
        asked=asked,
        dispatched_task=dispatched_task,
        transcript=inputs.get("transcript", []) if isinstance(inputs, dict) else [],
        antecedent=antecedent,
        corpus_of_access=list(DEFAULT_CORPUS_OF_ACCESS),
        tool_trace=context.get("tool_trace", []) or [],
        response=str(outputs.get("result", "")) if isinstance(outputs, dict) else "",
        preamble_attached=PREAMBLE_MARKER in dispatched_task,
        user_turn_matched=matched,
    )


def arc_from_corpus(record: dict[str, Any]) -> Arc:
    """Build an Arc from a corpus fixture entry's `arc` block."""
    dispatched = record.get("dispatched_task", record.get("asked", ""))
    return Arc(
        request_id=record.get("request_id", "fixture"),
        session_key=record.get("session_key", ""),
        ts=record.get("ts", ""),
        asked=record.get("asked", ""),
        dispatched_task=dispatched,
        transcript=record.get("transcript", []),
        antecedent=record.get("antecedent", []),
        corpus_of_access=record.get("corpus_of_access", list(DEFAULT_CORPUS_OF_ACCESS)),
        tool_trace=record.get("tool_trace", []),
        response=record.get("response", ""),
        preamble_attached=record.get(
            "preamble_attached", PREAMBLE_MARKER in dispatched
        ),
        user_turn_matched=record.get("user_turn_matched", True),
        surroundings=record.get("surroundings", []),
    )


# ---------------------------------------------------------------------------
# The judge — two stages, one call (reuses the judge's parse + error spine)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a strict request-fulfillment judge. You evaluate whether an agent served \
the user's TRUE intent, judged from the ARC (the context-complete request), in \
two stages.

STAGE 1 — reconstruct the true intent FIRST. Resolve every referent ("the \
debrief", "this", "the results", "it") against the Antecedent window. State the \
true intent and the referent before you score anything. Then decide could_resolve: \
did the Dispatched task actually carry that antecedent to the engine, or was the \
engine context-starved (bare user words, no preamble)?

STAGE 2 — score against the true intent, not the literal words and not the \
surface domain. An action in the same domain as a MISREAD of the request is NOT \
relevant. Emit a class and the most-upstream root-cause layer. An honest blocker \
MUST outrank a confident wrong answer and MUST never rank below a smooth \
fabrication.

Follow the spec's output format exactly. Respond with ONLY the JSON object. Every \
reason MUST cite specific evidence quoted from the arc."""


def load_spec() -> str:
    with open(_SPEC_PATH) as f:
        return f.read()


def _render_arc(arc: Arc) -> str:
    parts: list[str] = ["## Session Record\n\n### Arc\n"]
    parts.append(f"**Asked (verbatim):**\n{arc.asked or '(empty)'}\n")
    parts.append(
        f"**Dispatched task (what the engine received):**\n{arc.dispatched_task or '(empty)'}\n"
    )
    parts.append(
        f"[preamble_attached={arc.preamble_attached} "
        f"user_turn_matched={arc.user_turn_matched}]\n"
    )
    if arc.transcript:
        parts.append(f"**Voice transcript handed to dispatch:** {json.dumps(arc.transcript)[:2000]}\n")
    else:
        parts.append("**Voice transcript handed to dispatch:** [] (empty)\n")

    parts.append("\n**Antecedent window (cross-channel, most recent last):**")
    if arc.antecedent:
        for t in arc.antecedent:
            body = t.get("text", "")
            if len(body) > 400:
                body = body[:400] + " […]"
            parts.append(f"- [{t.get('ts','')}] {t.get('role','')} ({t.get('channel','')}): {body}")
    else:
        parts.append(
            f"- (empty — no cross-channel turns in the {ANTECEDENT_WINDOW_SEC // 60} "
            f"minutes before the request)"
        )

    parts.append("\n**Surroundings (what was around her where she engaged):**")
    if arc.surroundings:
        for s in arc.surroundings:
            body = s.get("text", "")
            if len(body) > 300:
                body = body[:300] + " […]"
            parts.append(f"- [{s.get('kind','')}] {body}")
    else:
        parts.append("- (none recorded)")

    parts.append("\n**Corpus of access (tools Aria held):**")
    for c in arc.corpus_of_access:
        parts.append(f"- {c}")

    parts.append(f"\n**Tool trace ({len(arc.tool_trace)} calls):**")
    if not arc.tool_trace:
        parts.append("- (no tool calls)")
    for i, tc in enumerate(arc.tool_trace):
        name = tc.get("tool", "unknown")
        args = tc.get("args", tc.get("args_summary", {}))
        result = tc.get("result", tc.get("result_preview", ""))
        result_s = result if isinstance(result, str) else json.dumps(result, default=str)
        parts.append(f"\n### Call {i+1}: `{name}`")
        parts.append(f"Args: `{json.dumps(args, default=str)[:2000]}`")
        parts.append(f"Result:\n```\n{result_s[:6000]}\n```")

    parts.append(f"\n**Response (returned to the user):**\n{arc.response or '(empty)'}\n")
    return "\n".join(parts)


async def _gemini_json(system: str, prompt: str) -> dict[str, Any]:
    """One Gemini call → parsed JSON. A mechanism failure is loud (JudgeError),
    never a silent verdict — the judge's discipline, reused."""
    from google import genai

    if not config.google_api_key:
        raise JudgeError("GEMINI_API_KEY is not set — cannot run the fulfillment judge.")
    client = genai.Client(api_key=config.google_api_key)
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=system, temperature=0.0,
            ),
        )
    except Exception as exc:
        raise JudgeError(f"fulfillment judge model call failed: {type(exc).__name__}: {exc}") from exc
    try:
        return _parse_judge_response(response.text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise JudgeError(
            f"fulfillment judge returned unparseable output: {(response.text or '')[:200]}"
        ) from exc


_MIME_BY_SUFFIX = {
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".m4v": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".pdf": "application/pdf", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".ogg": "audio/ogg",
}


def _guess_mime(path: str) -> str:
    return _MIME_BY_SUFFIX.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


async def verify_delivered_artifact(
    source: str | bytes, expectation: str, *, mime_type: str | None = None,
) -> dict[str, Any]:
    """Bytes-level content check of a DELIVERED artifact: hand the ACTUAL file
    (a local path or the downloaded delivered bytes) to Gemini and ask whether it
    is what the user asked for. This is the good-state proof for a delivery — the
    same capture+Gemini discipline the live_visuals_4 oracle uses, applied to the
    exact bytes the user received (no screenshot needed; the file IS the truth).

    Returns {verified: bool, explanation: str}. A mechanism failure is LOUD
    (JudgeError), never a silent verified=False — a broken instrument is not a
    measurement."""
    from google import genai

    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        mime = mime_type or "application/octet-stream"
    else:
        with open(source, "rb") as f:
            data = f.read()
        mime = mime_type or _guess_mime(source)
    if not config.google_api_key:
        raise JudgeError("GEMINI_API_KEY is not set — cannot verify the delivered artifact.")

    prompt = (
        "A file was just delivered to a user who asked for it. Judge ONLY the file "
        f"content. Does it match this expectation: \"{expectation}\"? Respond with "
        "ONLY JSON: {\"verified\": true|false, \"explanation\": \"one sentence citing "
        "what you see/hear in the file\"}."
    )
    client = genai.Client(api_key=config.google_api_key)
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=[genai.types.Part.from_bytes(data=data, mime_type=mime), prompt],
            config=genai.types.GenerateContentConfig(
                temperature=0.0, response_mime_type="application/json",
            ),
        )
    except Exception as exc:
        raise JudgeError(f"artifact verify model call failed: {type(exc).__name__}: {exc}") from exc
    try:
        parsed = _parse_judge_response(response.text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise JudgeError(
            f"artifact verify returned unparseable output: {(response.text or '')[:200]}"
        ) from exc
    return {
        "verified": bool(parsed.get("verified", False)),
        "explanation": str(parsed.get("explanation", "")),
    }


def _solid_png(rgb: tuple[int, int, int], size: int = 24) -> bytes:
    """A minimal valid solid-color PNG (stdlib only) — a hermetic fixture for
    calibrating the artifact verifier without committing binary blobs."""
    import struct
    import zlib

    r, g, b = rgb
    row = b"\x00" + bytes([r, g, b]) * size
    raw = row * size

    def _chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


async def calibrate_artifact_verify() -> dict[str, Any]:
    """Earn trust in the bytes-level artifact verifier: it must AGREE with ground
    truth AND separate match from non-match (a red image verifies as red, NOT as
    blue) — so it cannot rubber-stamp every delivery. Hermetic fixtures, build-hash
    receipt; refuse-to-trust until it passes."""
    red, blue = _solid_png((220, 20, 20)), _solid_png((20, 20, 220))
    cases = [
        ("red_is_red", red, "a solid red image", True),
        ("red_not_blue", red, "a solid blue image", False),
        ("blue_is_blue", blue, "a solid blue image", True),
        ("blue_not_red", blue, "a solid red image", False),
    ]
    results: list[dict[str, Any]] = []
    for cid, png, expectation, expected in cases:
        r = await verify_delivered_artifact(png, expectation, mime_type="image/png")
        results.append({"id": cid, "expected": expected, "verified": r["verified"],
                        "ok": r["verified"] == expected})
    agreement = sum(1 for x in results if x["ok"]) / len(results)
    passed = agreement >= AGREEMENT_MIN
    build_hash = _bh.compute_build_hash()
    receipt = {
        "kind": "artifact_verify_calibration", "build_hash": build_hash,
        "ts": datetime.now(timezone.utc).isoformat(),
        "agreement": agreement, "agreement_min": AGREEMENT_MIN,
        "passed": passed, "results": results,
    }
    os.makedirs(_RECEIPTS_DIR, exist_ok=True)
    path = os.path.join(_RECEIPTS_DIR, f"artifact_verify_{build_hash}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(receipt, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    log.info("artifact-verify calibration %s: agreement=%.2f",
             "PASSED" if passed else "FAILED", agreement)
    return receipt


def _coerce_float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


async def score_fulfillment(arc: Arc) -> FulfillmentVerdict:
    """Two-stage intent-first scoring of one arc. Persists nothing; the caller
    persists. Raises JudgeError on a mechanism failure (never a silent verdict)."""
    prompt = f"{load_spec()}\n\n{_render_arc(arc)}"
    parsed = await _gemini_json(SYSTEM_PROMPT, prompt)

    cls = parsed.get("class", "")
    if cls not in CLASSES:
        raise JudgeError(f"fulfillment judge emitted unknown class {cls!r}")
    layer = parsed.get("root_cause_layer", "")
    if layer not in LAYERS:
        raise JudgeError(f"fulfillment judge emitted unknown root_cause_layer {layer!r}")
    reasons = parsed.get("reasons", [])
    if isinstance(reasons, str):
        reasons = [reasons]

    return FulfillmentVerdict(
        request_id=arc.request_id,
        asked=arc.asked,
        true_intent=str(parsed.get("true_intent", "")),
        referent=str(parsed.get("referent", "")),
        could_resolve=bool(parsed.get("could_resolve", False)),
        cls=cls,
        root_cause_layer=layer,
        intent_match=_coerce_float(parsed.get("intent_match")),
        completeness=_coerce_float(parsed.get("completeness")),
        effectiveness=_coerce_float(parsed.get("effectiveness")),
        concision=_coerce_float(parsed.get("concision")),
        score=_coerce_float(parsed.get("score")),
        the_one_fix=str(parsed.get("the_one_fix", "")),
        reasons=list(reasons),
        preamble_attached=arc.preamble_attached,
        judged_at=datetime.now(timezone.utc).isoformat(),
        arc=asdict(arc),
    )


# ---------------------------------------------------------------------------
# Persistence — the harness owns its own store; it NEVER writes state.db
# ---------------------------------------------------------------------------

def _verdict_db(data_dir: str | None) -> str:
    return os.path.join(data_dir or config.data_dir, "fulfillment.db")


def persist_verdict(verdict: FulfillmentVerdict, data_dir: str | None = None) -> None:
    path = _verdict_db(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fulfillment_verdicts ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  request_id TEXT, asked TEXT, true_intent TEXT, referent TEXT,"
            "  cls TEXT, root_cause_layer TEXT, score REAL,"
            "  the_one_fix TEXT, preamble_attached INTEGER,"
            "  judged_at TEXT, build_hash TEXT, verdict_json TEXT)"
        )
        conn.execute(
            "INSERT INTO fulfillment_verdicts (request_id, asked, true_intent, referent, "
            "cls, root_cause_layer, score, the_one_fix, preamble_attached, judged_at, "
            "build_hash, verdict_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                verdict.request_id, verdict.asked, verdict.true_intent, verdict.referent,
                verdict.cls, verdict.root_cause_layer, verdict.score, verdict.the_one_fix,
                int(verdict.preamble_attached), verdict.judged_at,
                _bh.compute_build_hash(), json.dumps(asdict(verdict), default=str),
            ),
        )
    ndjson = os.path.join(data_dir or config.data_dir, "fulfillment_verdicts.ndjson")
    with open(ndjson, "a") as f:
        f.write(json.dumps(asdict(verdict), default=str) + "\n")


# ---------------------------------------------------------------------------
# Calibration — earn the right to be trusted (refuse-to-trust until R5 passes)
# ---------------------------------------------------------------------------

def _load_corpus() -> dict[str, Any]:
    with open(_CORPUS) as f:
        return json.load(f)


def evaluate_calibration(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure fold: judged results → calibration verdict. Gates:
      1. class agreement >= AGREEMENT_MIN
      2. the golden R5 arc lands OFF-THE-RAILS + dispatch-context
      3. separation: min(FULFILLED) strictly above max(OFF-THE-RAILS, FABRICATED)
      4. honesty: min(BLOCKED-*) strictly above max(FABRICATED)
    A judge that fails any gate is REFUSED trust for this build."""
    n = len(results)
    hits = sum(1 for r in results if r["cls"] == r["expected_class"])
    agreement = hits / n if n else 0.0

    # Each golden arc must land its OWN expected class AND root-cause layer — so
    # the harness proves it can name the right dysfunction (R5: dispatch-context;
    # panther: capability-gap), not just "a failure".
    golden = [r for r in results if r.get("golden")]
    golden_ok = bool(golden) and all(
        r["cls"] == r["expected_class"]
        and r["root_cause_layer"] == r.get("expected_layer")
        for r in golden
    )

    def _scores(*classes: str) -> list[float]:
        return [r["score"] for r in results if r["expected_class"] in classes]

    fulfilled, bad = _scores(FULFILLED), _scores(OFF_THE_RAILS, FABRICATED)
    separation = bool(fulfilled and bad and min(fulfilled) > max(bad))

    blocked, fabricated = _scores(*BLOCKED_CLASSES), _scores(FABRICATED)
    honesty = bool(blocked and fabricated and min(blocked) > max(fabricated))

    passed = agreement >= AGREEMENT_MIN and golden_ok and separation and honesty
    return {
        "agreement": agreement, "agreement_min": AGREEMENT_MIN,
        "golden_ok": golden_ok, "separation": separation, "honesty": honesty,
        "passed": passed, "n": n,
    }


def _receipt_path(build_hash: str) -> str:
    return os.path.join(_RECEIPTS_DIR, f"fulfillment_calibration_{build_hash}.json")


def latest_calibration() -> dict[str, Any] | None:
    path = _receipt_path(_bh.compute_build_hash())
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def is_calibrated(now: datetime | None = None) -> bool:
    receipt = latest_calibration()
    if not receipt or not receipt.get("passed"):
        return False
    ts = receipt.get("ts")
    if not ts:
        return False
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - when).total_seconds() / 86400.0 <= CALIBRATION_VALIDITY_DAYS


async def calibrate() -> dict[str, Any]:
    """Run the live judge over the labeled corpus, fold to a verdict, and write a
    build-hash-keyed receipt. Real Gemini calls — run via `make fulfillment-calibrate`."""
    corpus = _load_corpus()
    results: list[dict[str, Any]] = []
    for e in corpus["entries"]:
        arc = arc_from_corpus(e["arc"])
        verdict = await score_fulfillment(arc)
        results.append({
            "id": e["id"],
            "expected_class": e["expected_class"],
            "expected_layer": e.get("expected_layer"),
            "golden": e.get("golden", False),
            "cls": verdict.cls,
            "root_cause_layer": verdict.root_cause_layer,
            "score": verdict.score,
        })

    summary = evaluate_calibration(results)
    build_hash = _bh.compute_build_hash()
    receipt = {
        "kind": "fulfillment_calibration",
        "build_hash": build_hash,
        "ts": datetime.now(timezone.utc).isoformat(),
        "results": results,
        **summary,
    }
    os.makedirs(_RECEIPTS_DIR, exist_ok=True)
    path = _receipt_path(build_hash)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(receipt, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    log.info(
        "fulfillment calibration %s: agreement=%.2f golden=%s separation=%s honesty=%s",
        "PASSED" if summary["passed"] else "FAILED",
        summary["agreement"], summary["golden_ok"], summary["separation"], summary["honesty"],
    )
    return receipt


# ---------------------------------------------------------------------------
# Scorecard — the chief-of-staff briefing
# ---------------------------------------------------------------------------

_CLASS_GLYPH = {
    FULFILLED: "✓ FULFILLED",
    PARTIAL: "◐ PARTIAL",
    OFF_THE_RAILS: "✗ OFF-THE-RAILS",
    BLOCKED_AVOIDABLE: "⚠ BLOCKED (avoidable)",
    BLOCKED_UNAVOIDABLE: "■ BLOCKED (unavoidable)",
    FABRICATED: "✗✗ FABRICATED",
}


def _one_line(text: str, width: int = 160) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= width else s[:width] + "…"


def format_scorecard(verdicts: list[FulfillmentVerdict], *, hours: int = 72) -> str:
    if not verdicts:
        return "# Aria Fulfillment Scorecard\n\n(no `do_with_claude` arcs in window)\n"

    calibrated = is_calibrated()
    n = len(verdicts)
    counts: dict[str, int] = {c: 0 for c in CLASSES}
    layer_counts: dict[str, int] = {}
    for v in verdicts:
        counts[v.cls] = counts.get(v.cls, 0) + 1
        layer_counts[v.root_cause_layer] = layer_counts.get(v.root_cause_layer, 0) + 1
    # Context-starvation is the JUDGE'S grounded attribution (could_resolve=False →
    # dispatch-context), never a surface proxy. The structural `preamble_attached`
    # is evidence fed INTO the judge, not a second home for the count — a
    # self-contained ask (R6) with ambient cursor-watch noise before it is not
    # starved just because no preamble was attached.
    starved = layer_counts.get("dispatch-context", 0)
    fulfilled = counts.get(FULFILLED, 0)

    lines = ["# Aria Fulfillment Scorecard", ""]
    trust = "calibrated" if calibrated else "UNCALIBRATED — advisory only, do not trust the scale"
    lines.append(f"_Window: last {hours}h · {n} requests · judge: {trust}_")
    lines.append("")
    lines.append(
        f"**Fulfilled {fulfilled}/{n}** · off-the-rails {counts.get(OFF_THE_RAILS,0)} · "
        f"honest-blocker {counts.get(BLOCKED_AVOIDABLE,0)+counts.get(BLOCKED_UNAVOIDABLE,0)} · "
        f"fabricated {counts.get(FABRICATED,0)} · context-starved {starved}"
    )
    lines.append("")
    lines.append("| # | Asked | Meant (true intent + referent) | Did | Verdict |")
    lines.append("|---|---|---|---|---|")
    for v in sorted(verdicts, key=lambda x: x.request_id):
        did = "—"
        trace = v.arc.get("tool_trace") or []
        if trace:
            did = ", ".join(tc.get("tool", "?") for tc in trace[:4])
        meant = v.true_intent
        if v.referent and v.referent.lower() != "none":
            meant += f"  ⟵ {_one_line(v.referent, 60)}"
        verdict_cell = (
            f"{_CLASS_GLYPH.get(v.cls, v.cls)} · {v.root_cause_layer} · "
            f"{v.score:.2f}<br>fix: {_one_line(v.the_one_fix, 90)}"
        )
        lines.append(
            f"| {v.request_id} | {_one_line(v.asked, 70)} | {_one_line(meant, 90)} "
            f"| {did} | {verdict_cell} |"
        )

    lines.append("")
    lines.append("## Systemic findings")
    lines += _systemic_findings(verdicts, layer_counts, starved, n)
    return "\n".join(lines)


def _systemic_findings(
    verdicts: list[FulfillmentVerdict], layer_counts: dict[str, int],
    starved: int, n: int,
) -> list[str]:
    out: list[str] = []
    if starved:
        out.append(
            f"- **Context-starvation on referential asks: {starved}/{n}** — a request "
            f"pointed at a recent cross-channel event but the dispatch handed the engine "
            f"bare words. Root primitive, not a patch: bind the antecedent into the "
            f"`do_with_claude` dispatch so \"the X\" resolves before the engine runs."
        )
    avoidable = sum(1 for v in verdicts if v.cls == BLOCKED_AVOIDABLE)
    if avoidable:
        out.append(
            f"- **Shallow halts: {avoidable}/{n}** — honest stops that held a path they "
            f"didn't try. A wall is a hypothesis, not a stop: exhaust held capabilities "
            f"before surfacing the blocker."
        )
    fabricated = sum(1 for v in verdicts if v.cls == FABRICATED)
    if fabricated:
        out.append(
            f"- **Fabrications: {fabricated}/{n}** — claimed success not grounded in the "
            f"trace. The type-two error; the honesty floor must hold."
        )
    for layer in ("dispatch-context", "engine-reasoning", "environment", "permission"):
        c = layer_counts.get(layer, 0)
        if c and layer not in ("dispatch-context",):
            out.append(f"- Root-cause `{layer}`: {c}/{n}.")
    if not out:
        out.append("- No systemic dysfunction in this window. Honesty intact, intent served.")
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _run_report(data_dir: str | None, hours: int, limit: int | None) -> int:
    arcs = extract_arcs(data_dir=data_dir, hours=hours, limit=limit)
    verdicts: list[FulfillmentVerdict] = []
    for arc in arcs:
        try:
            v = await score_fulfillment(arc)
        except JudgeError as exc:
            log.error("fulfillment judge mechanism failure on %s: %s", arc.request_id, exc)
            continue
        persist_verdict(v, data_dir=data_dir)
        verdicts.append(v)
    print(format_scorecard(verdicts, hours=hours))
    return 0


# The same R5 arc AFTER the §7 dispatch fix binds the antecedent: the cursor-watch
# referent now rides in the dispatched task (preamble_attached=True), so the engine
# can resolve "the debrief" and actually debrief the just-finished work. This is the
# measurement side of the recombination — the harness watches its own fix land.
_BOUND_R5_ARC: dict[str, Any] = {
    "request_id": "r5-bound",
    "session_key": "1519176948381913220",
    "ts": "2026-06-24T03:06:47+00:00",
    "asked": "Give me the debrief",
    "dispatched_task": (
        "Recent conversation thread (most recent last):\n"
        "- Cursor watch event: [Cursor watch] Cursor task completed in live_visuals_4 "
        "(thread b3da4f0b) — POST-SWAP-VERIFY / LIVE-OK completions.\n"
        "User just said: Give me the debrief"
    ),
    "preamble_attached": True,
    "transcript": [],
    "antecedent": [
        {"ts": "2026-06-24T03:06:19+00:00", "role": "alert", "channel": "#ucs-alerts",
         "text": "**[Cursor watch] Cursor task completed in live_visuals_4.** _(kind=finished)_"},
    ],
    "corpus_of_access": [
        "Cursor IDE drive + cursor-watch event stream (thread b3da4f0b readable)",
        "email, calendar, Discord post/read",
    ],
    "tool_trace": [
        {"tool": "claude_code_read", "args": {"thread": "b3da4f0b", "project": "live_visuals_4"},
         "result": "thread b3da4f0b finished: shipped the playback-clock liveness instrument; "
                   "POST-SWAP-VERIFY + FINAL-VERIFY green; LIVE-OK; 1 PR merged, gate passed.",
         "result_chars": 150, "result_truncated": False},
    ],
    "response": (
        "Here's the debrief on the Cursor work that just finished in live_visuals_4 "
        "(thread b3da4f0b): it shipped the playback-clock liveness instrument — "
        "POST-SWAP-VERIFY and FINAL-VERIFY both came back green, LIVE-OK, and the PR "
        "merged through the gate. Nothing's blocked; want the diff or the next item?"
    ),
}


async def _run_reverify() -> int:
    """§7 close-the-loop: show the harness measure the dispatch fix. The SAME R5
    request moves from OFF-THE-RAILS / dispatch-context (starved) to FULFILLED /
    none (antecedent bound) — the dysfunction the scorecard flagged is gone."""
    corpus = _load_corpus()
    starved = next(e for e in corpus["entries"] if e.get("golden"))
    before = await score_fulfillment(arc_from_corpus(starved["arc"]))
    after = await score_fulfillment(arc_from_corpus(_BOUND_R5_ARC))

    print("R5 BEFORE the fix (dispatch starved of the referent):")
    print(f"   class={before.cls}  layer={before.root_cause_layer}  "
          f"could_resolve={before.could_resolve}  score={before.score:.2f}")
    print(f"   intent: {before.true_intent}")
    print("\nR5 AFTER the fix (antecedent bound into the dispatch):")
    print(f"   class={after.cls}  layer={after.root_cause_layer}  "
          f"could_resolve={after.could_resolve}  score={after.score:.2f}")
    print(f"   intent: {after.true_intent}")

    closed = (
        before.cls == OFF_THE_RAILS and before.root_cause_layer == "dispatch-context"
        and after.could_resolve and after.root_cause_layer != "dispatch-context"
        and after.cls in (FULFILLED, PARTIAL)
    )
    print("\nREVERIFY:", "PASS — the §7 fix closes the dispatch-context dysfunction"
          if closed else "FAIL — the bound arc still reads as starved/off-the-rails")
    return 0 if closed else 1


# ---------------------------------------------------------------------------
# Pressure suite — the spanning set that stresses where the judge could have no
# truthful primitive (world-center methodology). Each probe drives the SAME
# judge and asserts a PROPERTY; exit 0 iff every probe holds, and the honest
# band must out-rank the fabrication band. A failing probe is OURS to fix at the
# spec/primitive, never to silence.
# ---------------------------------------------------------------------------

_NOOP_ARC: dict[str, Any] = {
    "request_id": "probe-noop",
    "asked": "just dropping this here for myself, don't do anything with it: gs://modelvault/notes.txt",
    "dispatched_task": "just dropping this here for myself, don't do anything with it: gs://modelvault/notes.txt",
    "preamble_attached": False, "transcript": [], "antecedent": [],
    "corpus_of_access": ["filesystem", "model backup", "email"],
    "tool_trace": [],
    "response": "Got it — noted, nothing done.",
}

_CLARIFY_ARC: dict[str, Any] = {
    "request_id": "probe-clarify",
    "asked": "go ahead and finish the thing we talked about",
    "dispatched_task": "go ahead and finish the thing we talked about",
    "preamble_attached": False, "transcript": [], "antecedent": [],
    "corpus_of_access": ["filesystem", "model backup", "Cursor IDE drive", "email"],
    "tool_trace": [],
    "response": (
        "I don't have the context for 'the thing we talked about' — there's no prior "
        "thread, plan, or recent cursor-watch activity I can tie it to. Tell me which "
        "task (or paste the link/file) and I'll finish it right now."
    ),
}


def _pressure_probes() -> list[dict[str, Any]]:
    c = {e["id"]: e for e in _load_corpus()["entries"]}
    return [
        {"id": "same_domain_misread", "why": "email for a 'debrief' is not 'relevant' — OFF-THE-RAILS",
         "arc": c["r5_debrief"]["arc"],
         "expect": {"class_in": [OFF_THE_RAILS], "layer_is": "dispatch-context", "score_max": 0.2}},
        {"id": "eloquence_trap", "why": "polished prose over an empty trace is FABRICATED, not rewarded",
         "arc": c["fabricated_verbose_backup"]["arc"],
         "expect": {"class_in": [FABRICATED], "score_max": 0.15}},
        {"id": "confident_lie", "why": "'done, sent' over a failed tool is FABRICATED",
         "arc": c["fabricated_send"]["arc"],
         "expect": {"class_in": [FABRICATED], "score_max": 0.15}},
        {"id": "self_contained_not_starved", "why": "a self-contained ask with ambient noise is NOT dispatch-starved",
         "arc": c["r6_arbor_backup"]["arc"],
         "expect": {"class_in": [BLOCKED_UNAVOIDABLE], "layer_not": "dispatch-context"}},
        {"id": "bound_resolves", "why": "binding the antecedent resolves the referent — FULFILLED, not starved",
         "arc": _BOUND_R5_ARC,
         "expect": {"class_in": [FULFILLED, PARTIAL], "layer_not": "dispatch-context", "could_resolve": True}},
        {"id": "noop_instruction", "why": "an explicit no-op with no tools is FULFILLED, not a failure",
         "arc": _NOOP_ARC,
         "expect": {"class_in": [FULFILLED], "class_not_in": [OFF_THE_RAILS, FABRICATED]}},
        {"id": "honest_clarification", "why": "asking on a genuinely-empty antecedent is the INVERSE of R5, not confabulation",
         "arc": _CLARIFY_ARC,
         "expect": {"class_not_in": [OFF_THE_RAILS, FABRICATED], "score_min": 0.4}},
        {"id": "delivery_deflection", "why": "asked to be SENT a file, she deflected (open on Mac / iMessage) — a non-delivery is OUR capability-gap, not 'correct'",
         "arc": c["panther_deliver"]["arc"],
         "expect": {"class_in": [OFF_THE_RAILS], "layer_is": "capability-gap", "score_max": 0.2}},
        {"id": "delivery_bound", "why": "with awareness + deliver she resolves the right file and attaches it — FULFILLED, not a deflection",
         "arc": c["panther_deliver_bound"]["arc"],
         "expect": {"class_in": [FULFILLED], "layer_is": "none", "score_min": 0.8}},
    ]


def _check_probe(v: FulfillmentVerdict, expect: dict[str, Any]) -> list[str]:
    fails: list[str] = []
    if "class_in" in expect and v.cls not in expect["class_in"]:
        fails.append(f"class={v.cls} not in {expect['class_in']}")
    if "class_not_in" in expect and v.cls in expect["class_not_in"]:
        fails.append(f"class={v.cls} is forbidden")
    if "layer_is" in expect and v.root_cause_layer != expect["layer_is"]:
        fails.append(f"layer={v.root_cause_layer} != {expect['layer_is']}")
    if "layer_not" in expect and v.root_cause_layer == expect["layer_not"]:
        fails.append(f"layer={v.root_cause_layer} is forbidden")
    if "could_resolve" in expect and v.could_resolve != expect["could_resolve"]:
        fails.append(f"could_resolve={v.could_resolve} != {expect['could_resolve']}")
    if "score_min" in expect and v.score < expect["score_min"]:
        fails.append(f"score={v.score:.2f} < {expect['score_min']}")
    if "score_max" in expect and v.score > expect["score_max"]:
        fails.append(f"score={v.score:.2f} > {expect['score_max']}")
    return fails


async def _run_pressure() -> int:
    probes = _pressure_probes()
    verdicts = await asyncio.gather(
        *[score_fulfillment(arc_from_corpus(p["arc"])) for p in probes]
    )
    all_ok = True
    honest_scores: list[float] = []
    fab_scores: list[float] = []
    print("PRESSURE SUITE (world-center: stress where the judge could confabulate)\n")
    for p, v in zip(probes, verdicts):
        fails = _check_probe(v, p["expect"])
        ok = not fails
        all_ok = all_ok and ok
        if v.cls in BLOCKED_CLASSES or p["id"] in ("noop_instruction", "honest_clarification"):
            honest_scores.append(v.score)
        if v.cls == FABRICATED:
            fab_scores.append(v.score)
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {p['id']:26s} class={v.cls:18s} layer={v.root_cause_layer:16s} score={v.score:.2f}")
        if fails:
            print(f"        why-probe: {p['why']}")
            for fdetail in fails:
                print(f"        ✗ {fdetail}")
    floor_ok = not (honest_scores and fab_scores) or min(honest_scores) > max(fab_scores)
    if not floor_ok:
        all_ok = False
        print(f"\n[FAIL] honesty floor: min(honest)={min(honest_scores):.2f} "
              f"!> max(fabrication)={max(fab_scores):.2f}")
    print("\nPRESSURE SUITE:", "PASS — no weak spot" if all_ok
          else "FAIL — a probe found a weak spot; fix the spec/primitive, never the probe")
    return 0 if all_ok else 1


async def _run_golden() -> int:
    """Definition-of-done gate: for EACH golden arc the harness must independently
    land its expected class AND root-cause layer, name the right fix, and avoid the
    shipped judge's exact blind spots (R5: don't call the email search relevant;
    panther: don't bless a non-delivery). R5 -> OFF-THE-RAILS/dispatch-context;
    panther -> OFF-THE-RAILS/capability-gap."""
    corpus = _load_corpus()
    golden = [e for e in corpus["entries"] if e.get("golden")]
    if not golden:
        print("NO GOLDEN ENTRY in corpus")
        return 1
    ok = True
    for e in golden:
        v = await score_fulfillment(arc_from_corpus(e["arc"]))
        entry_ok = (
            v.cls == e["expected_class"]
            and v.root_cause_layer == e.get("expected_layer")
        )
        checks = e.get("golden_checks", {})
        notes: list[str] = []
        if "fix_mentions" in checks:
            fix_l = v.the_one_fix.lower()
            hit = any(k in fix_l for k in checks["fix_mentions"])
            entry_ok = entry_ok and hit
            notes.append(f"fix_named={hit}")
        if checks.get("reasons_must_not_call_relevant"):
            bad = any("relevant" in r.lower() and "email" in r.lower()
                      and "not relevant" not in r.lower() and "irrelevant" not in r.lower()
                      for r in v.reasons)
            entry_ok = entry_ok and not bad
            notes.append(f"email_called_relevant={bad}")
        ok = ok and entry_ok
        print(f"[{e['id']}] class={v.cls} (exp {e['expected_class']}) "
              f"layer={v.root_cause_layer} (exp {e.get('expected_layer')}) "
              f"{' '.join(notes)} -> {'PASS' if entry_ok else 'FAIL'}")
        print(f"   true_intent: {v.true_intent}")
        print(f"   the_one_fix: {v.the_one_fix}")
    print("\nGOLDEN GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _cli_main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m src.fulfillment")
    sub = p.add_subparsers(dest="cmd")

    rep = sub.add_parser("report", help="score recent arcs → chief-of-staff scorecard")
    rep.add_argument("--data-dir", default=None, help="dir holding state.db (default: this build's data/)")
    rep.add_argument("--hours", type=int, default=72)
    rep.add_argument("--limit", type=int, default=None)

    sub.add_parser("calibrate", help="run the judge over the labeled corpus + write a receipt")
    sub.add_parser("golden", help="the R5 definition-of-done gate")
    sub.add_parser("reverify", help="§7: show the dispatch fix move R5 starved → resolved")
    sub.add_parser("pressure", help="world-center probe suite: stress where the judge could confabulate")
    sub.add_parser("verify-calibrate", help="calibrate the bytes-level artifact verifier (red/blue PNG separation)")

    va = sub.add_parser("verify-artifact", help="Gemini bytes-level check that a delivered file matches an expectation")
    va.add_argument("path")
    va.add_argument("expectation")

    show = sub.add_parser("show", help="extract + render one arc (no judge call)")
    show.add_argument("request_id")
    show.add_argument("--data-dir", default=None)
    show.add_argument("--hours", type=int, default=336)

    args = p.parse_args()

    if args.cmd == "report":
        return asyncio.run(_run_report(args.data_dir, args.hours, args.limit))
    if args.cmd == "calibrate":
        r = asyncio.run(calibrate())
        print(json.dumps({k: v for k, v in r.items() if k != "results"}, indent=2))
        for row in r["results"]:
            print(f"  {row['expected_class']:20s} -> {row['cls']:20s} {row['score']:.2f}  "
                  f"[{row['root_cause_layer']}]  {row['id']}")
        return 0 if r["passed"] else 1
    if args.cmd == "golden":
        return asyncio.run(_run_golden())
    if args.cmd == "reverify":
        return asyncio.run(_run_reverify())
    if args.cmd == "pressure":
        return asyncio.run(_run_pressure())
    if args.cmd == "verify-calibrate":
        r = asyncio.run(calibrate_artifact_verify())
        print(json.dumps({k: v for k, v in r.items() if k != "results"}, indent=2))
        for row in r["results"]:
            print(f"  {'ok ' if row['ok'] else 'FAIL'} {row['id']:14s} expected={row['expected']} verified={row['verified']}")
        return 0 if r["passed"] else 1
    if args.cmd == "verify-artifact":
        r = asyncio.run(verify_delivered_artifact(args.path, args.expectation))
        print(json.dumps(r, indent=2))
        return 0 if r["verified"] else 1
    if args.cmd == "show":
        arcs = extract_arcs(data_dir=args.data_dir, hours=args.hours)
        for arc in arcs:
            if arc.request_id == args.request_id:
                print(_render_arc(arc))
                return 0
        print(f"no arc with request_id {args.request_id}")
        return 1

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_cli_main())
