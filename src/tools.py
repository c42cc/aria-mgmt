"""Tool implementations dispatched by Gemini function calling."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo

import anthropic

from .config import config
from .cursor_bridge import CursorBridge
from .db import (
    append_planning_history,
    get_active_cursor_sessions,
    get_daily_spend,
    get_findings,
    get_ground,
    get_planning_history,
    log_event,
    log_loop_execution,
    record_session,
    save_findings,
    set_ground,
    tool_to_product,
    upsert_cursor_session,
)
from .cursor_registry import cursor_registry
from .cursor_tools import (
    _cursor_agents,
    _cursor_read,
    _cursor_screenshot,
    _cursor_send,
    _cursor_spawn,
    _cursor_status_new,
    _cursor_threads,
)
from .claude_code import (
    _claude_code_read,
    _claude_code_send,
    _claude_code_spawn,
    _claude_code_threads,
)
from .capability import unverified_world_changes
from .conversation import conversation
from .outcomes import (
    TRANSIENT_RETRY_BUDGET,
    _action_family,
    classify_outcome,
    format_block,
    is_discovery_family,
)
from .memory import recall as mem_recall, remember as mem_remember, forget as mem_forget
from .prompts import (
    clear_cache as prompts_clear_cache,
    get_versions as prompts_get_versions,
    list_templates,
    load_template,
    read_raw,
    rollback_template,
    save_template,
)
from . import spark

log = logging.getLogger(__name__)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_TRACE_ARG_MAX_CHARS = 10_000
_TRACE_RESULT_MAX_CHARS = 1_000_000
_TRACE_SESSION_MAX_BYTES = 5_000_000

# P1 — Pre-send Grounding Gate. When Aria's draft reaches end_turn we re-run
# the anchor system against the trace; if any anchor reports
# `degraded`/`failed` we feed the violations back as a user message and let
# her revise. Each retry costs one extra agent iteration; cap is independent
# from the main iteration budget so retries can't be starved.
_GROUND_CHECK_MAX_RETRIES = 2
_GROUND_CHECK_VIOLATIONS_MAX_CHARS = 4_000

# P3 — Deterministic context injection. We build a small `<context>` block
# per call so Aria never has to fetch fallible facts (date, primary mail
# source, active capabilities, remaining budget) from a tool.
_LOCAL_TZ = ZoneInfo("America/Los_Angeles")
_USER_PRIMARY_EMAIL = os.getenv("DISCORD_EMAIL", "c@c42.io")


def _dedup_key(tool_name: str, args: dict) -> str:
    """Stable hash for (tool, args) pairs.

    Used by `_do_with_claude` (and the UCS variant) to detect when Claude
    re-emits an identical tool call inside one agent loop. Args are normalized
    to JSON with sorted keys so `{"a": 1, "b": 2}` and `{"b": 2, "a": 1}` hash
    to the same key.
    """
    try:
        args_json = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        args_json = repr(args)
    return f"{tool_name}:{args_json}"


# Hard ceiling on what a *single* agent loop may spend before it stops and
# checks in. The `max_tokens_budget` output-token guard never bounds cost
# because cost is dominated by *input* tokens — each iteration resends the
# growing message history, so a 30-iteration grind reached ~1.3M input tokens
# / ~$20 (the entire daily cap) in one call. This is the missing dollar floor.
# The loop projects the NEXT iteration's cost from the last one and stops
# BEFORE crossing the line (the honeycomb run charged $6.00 against this $5.00
# cap because the check ran after the spend).
_LOOP_COST_CAP_USD = float(os.getenv("DO_WITH_CLAUDE_LOOP_COST_CAP_USD", "5.0"))

# Backstop on blind discovery: if a loop has spent this much and EVERY tool
# call so far is a discovery action (find/grep/ls/search_files/…), the task's
# referent is unresolved and more searching is a grind — stop and ask the one
# question instead. With ground + the projects map in context this should
# almost never fire; it exists so an un-grounded referent costs ~$1.50 and one
# crisp question, not $5+ and a budget wall (honeycomb forensic 2026-06-12).
_DISCOVERY_COST_CAP_USD = float(os.getenv("DO_WITH_CLAUDE_DISCOVERY_CAP_USD", "1.5"))

# Carried-forward context discipline: tool results older than the last
# _COMPACT_KEEP_FULL tool-result messages are clipped to their head. The full
# text was visible to the model when fresh (it acted on it); re-billing a 30KB
# directory dump on every later iteration is what drove the honeycomb run from
# $0.49/step to $1.17/step.
_COMPACT_KEEP_FULL = 2
_COMPACT_HEAD_CHARS = 2_000

# Anthropic prompt-caching price multipliers (relative to base input price):
# a cache write costs 1.25x once; a cache read costs 0.10x. The static prefix
# (system prompt + tool catalog ≈ 20K tokens) and the growing message history
# are marked with cache breakpoints, so iteration N re-reads what iteration
# N-1 paid for instead of re-buying it at full price.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10


def _truncate_trace_args(args: dict, max_chars: int = _TRACE_ARG_MAX_CHARS) -> dict:
    """Truncate tool arg values for trace storage. Preserves keys and types."""
    out = {}
    for k, v in args.items():
        sv = str(v)
        if len(sv) > max_chars:
            out[k] = sv[:max_chars] + f"... [{len(sv)} chars total]"
        else:
            out[k] = v
    return out


def _rel_age(iso_ts: str) -> str:
    """Render an ISO timestamp as a short relative age ('3m ago', '2h ago')."""
    try:
        then = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return "unknown age"
    secs = max(0, (datetime.now(timezone.utc) - then).total_seconds())
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{int(secs // 60)}m ago"
    if secs < 129600:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _build_context(session_key: str = "") -> str:
    """Compose a deterministic `<context>` block for the agent's user message.

    P3: this block is recomputed every call. It supplies facts Aria
    historically guessed (date, today, primary mail source, what tools are
    actually available) so she never has to fetch them via a fallible
    upstream tool. Failures here are non-fatal — we always return at least
    the time line.

    The `projects:` and `ground:` sections are the ground primitive (forensic
    2026-06-12, the honeycomb thread): the loop must never pay Opus prices to
    discover a path the system already knows, and referents like "the plan"
    must resolve from durable state, not filesystem archaeology.
    """
    lines: list[str] = ["<context>"]

    now_local = datetime.now(_LOCAL_TZ)
    day_name = now_local.strftime("%A")
    lines.append(
        f"  now: {now_local.isoformat(timespec='seconds')}  "
        f"({day_name}, America/Los_Angeles)"
    )
    lines.append(f"  user_primary_email: {_USER_PRIMARY_EMAIL}")
    lines.append("  primary_mail_source: gmail  "
                 "(apple mail is filtered out — do not propose mail_messages)")

    if PROJECT_REGISTRY:
        lines.append("  projects (name → absolute path; NEVER search the "
                     "filesystem for a path listed here):")
        for name in sorted(PROJECT_REGISTRY):
            path = PROJECT_REGISTRY[name]
            missing = "" if os.path.isdir(path) else "  [MISSING ON DISK]"
            lines.append(f"    - {name} → {path}{missing}")

    try:
        bindings = get_ground()
        if bindings:
            lines.append("  ground (durable working set — resolve referents "
                         "like 'the plan' / 'that project' here first; update "
                         "via set_ground):")
            for b in bindings:
                bits = [b["label"]]
                if b.get("path"):
                    bits.append(b["path"])
                if b.get("detail"):
                    bits.append(b["detail"])
                lines.append(
                    f"    - {b['role']}: {' — '.join(bits)}  "
                    f"({_rel_age(b['updated_at'])})"
                )
    except Exception:
        log.warning("context: ground omitted", exc_info=True)

    try:
        from .mcp import mcp_client
        if mcp_client is not None and mcp_client._tools:
            by_server: dict[str, list[str]] = {}
            for tool_name, spec in mcp_client._tools.items():
                server = spec.get("server", "?")
                by_server.setdefault(server, []).append(tool_name)
            lines.append("  capabilities:")
            for server in sorted(by_server):
                tools = sorted(by_server[server])
                # Cap each server's tool list to keep tokens bounded.
                shown = tools[:8]
                more = f", +{len(tools)-len(shown)} more" if len(tools) > len(shown) else ""
                lines.append(f"    - {server}: {', '.join(shown)}{more}")
    except Exception:
        log.debug("context: MCP capabilities omitted", exc_info=True)

    # Precondition awareness: surface capabilities whose precondition is unmet
    # RIGHT NOW so the model drops them from its options instead of discovering
    # the wall by failing on it (the Messages-Automation thrash). capability_for
    # is the one home for "can I use this" — read here at planning time and at
    # dispatch time, never two homes.
    try:
        from .capability import capability_for
        unmet_caps: list[str] = []
        for server, tool, label in (
            ("apple", "messages_chat", "apple Messages send"),
            ("apple", "contacts_lookup", "apple Contacts"),
        ):
            fix = capability_for(server, tool).unmet()
            if fix:
                unmet_caps.append(f"    - {label} — UNAVAILABLE: {fix}")
        if unmet_caps:
            lines.append(
                "  unavailable (precondition unmet — do NOT choose these; surface "
                "the fix to the user instead of attempting and walling):"
            )
            lines.extend(unmet_caps)
    except Exception:
        log.debug("context: capability preconditions omitted", exc_info=True)

    try:
        remaining = max(0.0, config.daily_spend_cap_usd - get_daily_spend())
        lines.append(
            f"  budget_today_remaining_usd: {remaining:.2f}  "
            f"(cap ${config.daily_spend_cap_usd:.2f})"
        )
    except Exception:
        log.debug("context: budget omitted", exc_info=True)

    if session_key:
        lines.append(f"  session_key: {session_key}")

    lines.append("</context>")
    return "\n".join(lines) + "\n\n"


def _synth_anchor_record(
    tool_trace: list[dict], result: str, session_key: str
) -> dict:
    """Build the minimal record dict that `judge._run_anchors` consumes.

    `_run_anchors` reads `context_json.tool_trace` (the calls Aria made) and
    `outputs_json.result` (Aria's draft text). Both must be JSON-encoded
    strings — the judge always re-decodes. Everything else is metadata that
    anchors do not read but that keeps the record shape consistent with
    `_emit_session_record`.
    """
    return {
        "tool_name": "do_with_claude",
        "product": "agent",
        "timestamp": _now_iso(),
        "session_key": session_key,
        "inputs_json": "{}",
        "context_json": json.dumps({"tool_trace": tool_trace}, default=str),
        "outputs_json": json.dumps({"result": result}, default=str),
    }


def _summarize_anchor_violations(reports: list[dict]) -> str:
    """Render anchor reports into a fix-this block, or '' if no draft revision is needed.

    Only `degraded` / `failed` reports drive a retry. `correct` and
    `unverified` are passed through silently — `unverified` means the
    anchor's source-of-truth was unreachable and is not Aria's fault.
    """
    lines: list[str] = []
    for r in reports:
        binary = r.get("binary")
        if binary not in ("degraded", "failed"):
            continue
        tool = r.get("tool", "?")
        lines.append(f"\n### Anchor on `{tool}` — anchor verdict: {binary}")
        for v in r.get("violations", []):
            prop = v.get("prop", "?")
            sev = v.get("severity", "?")
            detail = v.get("detail", "")
            lines.append(f"- spec#{prop} [{sev}]: {detail}")
        for f in r.get("facts", []):
            key = f.get("key", "")
            if key in (
                "ground_truth_count",
                "aria_claimed_count",
                "tolerance",
                "missing_items",
            ):
                val = f.get("value")
                src = f.get("source", "")
                lines.append(f"  - {key}: {val} (source: {src})")
    if not lines:
        return ""
    body = "\n".join(lines)
    if len(body) > _GROUND_CHECK_VIOLATIONS_MAX_CHARS:
        body = body[:_GROUND_CHECK_VIOLATIONS_MAX_CHARS] + "\n... [violations truncated]"
    return body


async def _ground_check(
    tool_trace: list[dict], result: str, session_key: str
) -> str:
    """Run anchors against the agent's draft. Return non-empty fix-this text iff revision is required.

    Failures inside the anchor run are logged but never block delivery —
    they are the system's bug, not the agent's. A real `degraded`/`failed`
    anchor verdict, however, IS the agent's bug and produces a retry.
    """
    if not tool_trace:
        return ""
    try:
        from .judge import _run_anchors
        record = _synth_anchor_record(tool_trace, result, session_key)
        reports = await _run_anchors(record)
    except Exception:
        log.exception(
            "ground-check anchor run raised; delivering draft without gate session=%s",
            session_key,
        )
        return ""
    return _summarize_anchor_violations(reports)


def _ground_check_user_message(violations: str) -> str:
    """Compose the synthetic user message that asks Aria to revise."""
    return (
        "[GROUND-CHECK FAILED]\n\n"
        "Deterministic anchors re-queried the source of truth and disagree "
        "with the draft you just produced:\n"
        f"{violations}\n\n"
        "Revise your response so every count, date, and coverage claim "
        "matches the anchor facts above. If a claim cannot be supported "
        "by the tool trace or the anchor facts, remove it. Do not include "
        "this `[GROUND-CHECK FAILED]` message in your reply."
    )


def _cap_trace_size(trace: list[dict], max_bytes: int = _TRACE_SESSION_MAX_BYTES) -> list[dict]:
    """If total trace JSON exceeds max_bytes, drop oldest entries but keep a dropped count."""
    total = sum(len(json.dumps(e, default=str)) for e in trace)
    if total <= max_bytes:
        return trace
    dropped = 0
    while total > max_bytes and len(trace) > 1:
        removed = trace.pop(0)
        total -= len(json.dumps(removed, default=str))
        dropped += 1
    if dropped:
        trace.insert(0, {"_dropped_tool_calls": dropped, "_reason": "session trace exceeded 50KB cap"})
    return trace


_model_costs_cache: tuple[float, float] | None = None


def _get_model_costs() -> tuple[float, float]:
    """Read cost_per_m_input/output for the active Claude model from models.yaml.

    No silent fallback. If models.yaml is missing/unparseable or the active
    model isn't listed, raise — the daily-spend cap is computed off this and
    must not be fed lies. preflight `probe_models_yaml` is the boot-time
    check; this is the runtime version.

    Cached after first successful read. Invalidated by `_reload_prompts`
    (which is the user-facing "reload runtime config" hook) so a model
    swap via .env + !reload picks up new costs.
    """
    global _model_costs_cache
    if _model_costs_cache is not None:
        return _model_costs_cache
    import yaml
    with open(config.models_config) as f:
        data = yaml.safe_load(f)
    for spec in (data or {}).get("models", {}).values():
        if spec.get("model_id") == config.claude_model:
            cost_in = spec.get("cost_per_m_input")
            cost_out = spec.get("cost_per_m_output")
            if cost_in is None or cost_out is None:
                raise RuntimeError(
                    f"models.yaml entry for {config.claude_model} is missing "
                    f"cost_per_m_input or cost_per_m_output"
                )
            _model_costs_cache = (cost_in, cost_out)
            return _model_costs_cache
    raise RuntimeError(
        f"models.yaml has no entry whose model_id == {config.claude_model!r}. "
        f"Edit models.yaml or change CLAUDE_MODEL in .env."
    )


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Dollar cost of one model call, cache-aware.

    With prompt caching on, `usage.input_tokens` excludes cached tokens —
    they arrive as cache_creation (1.25x input price, paid once) and
    cache_read (0.10x). All four streams must be billed or the spend caps
    are fed lies.
    """
    cost_in, cost_out = _get_model_costs()
    return (input_tokens / 1_000_000 * cost_in
            + cache_creation_tokens / 1_000_000 * cost_in * _CACHE_WRITE_MULT
            + cache_read_tokens / 1_000_000 * cost_in * _CACHE_READ_MULT
            + output_tokens / 1_000_000 * cost_out)


def _usage_cost(usage: Any) -> float:
    """Bill an Anthropic `usage` object through `_estimate_cost`."""
    return _estimate_cost(
        getattr(usage, "input_tokens", 0) or 0,
        getattr(usage, "output_tokens", 0) or 0,
        getattr(usage, "cache_creation_input_tokens", 0) or 0,
        getattr(usage, "cache_read_input_tokens", 0) or 0,
    )


def _usage_context_tokens(usage: Any) -> int:
    """The TRUE context size of one call: fresh + cache-written + cache-read
    input tokens. `usage.input_tokens` alone under-reports once caching is on,
    which would corrupt the loop_executions telemetry the forensics read."""
    return (
        (getattr(usage, "input_tokens", 0) or 0)
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    )


_anthropic_client: anthropic.Anthropic | None = None
_cursor_bridge: CursorBridge | None = None
_post_callback: Callable[..., Coroutine] | None = None
_alert_callback: Callable[..., Coroutine] | None = None
_thread_callback: Callable[..., Coroutine] | None = None
_cursor_event_callback: Callable[..., Coroutine] | None = None
_reconnect_callback: Callable[..., Coroutine] | None = None
# Progress-spine sink. Injected by bot.py; the agent loop calls it before each
# tool dispatch so a long task narrates itself live to the originating channel
# (and keeps the voice session warm) instead of leaving the user staring at a
# three-dot loading indicator.
_progress_callback: Callable[..., Coroutine] | None = None
# Recommend-an-approach sink. Injected by bot.py; posts a tap-to-approve card
# to Corbin's phone and, on approval, runs the task autonomously. This is where
# human decisions live now that per-command confirmation is off.
_propose_callback: Callable[..., Coroutine] | None = None
# Blocking free-text question sink. Injected by bot.py; posts a question to the
# user and blocks for their typed/spoken reply (returns the answer text). Used
# when a task — including a Claude Code thread surfaced through Aria — needs an
# open answer mid-flight, not just a yes/no.
_ask_callback: Callable[..., Coroutine] | None = None
# Discord text-history fetchers injected at boot. Wired by bot.py so the
# tools layer doesn't take a hard dependency on the discord client.
_discord_history_callback: Callable[..., Coroutine] | None = None
_discord_threads_callback: Callable[..., Coroutine] | None = None
_transcript_provider: Callable[[], list[dict[str, str]]] | None = None


@dataclass
class SessionState:
    """Per-session mutable state for the tool dispatch loop.

    Pre-L2 these were module globals (`_cancel`, `_session_claude_calls`,
    `_session_cursor_runs`, `_last_tool_trace`, plus the agent lock). Two
    concurrent sessions (voice + text, or two channels) would clobber each
    other: A's `!stop` was silently cleared by B's loop entry, B's call
    counter inherited A's tail, B's `_last_tool_trace` overwrote A's right
    before A recorded its session row.

    Keyed by `session_key` (Discord channel/thread ID) because agent loops
    are parallel across channels. This is the correct asymmetry with
    VoiceController's global lock in discord_voice.py: Discord allows only
    one voice connection per bot, so voice is exclusive at the process level.

    Lives for the process lifetime; if memory pressure becomes an issue,
    prune on a timer — for now we expect O(few) sessions.
    """
    cancel: bool = False
    claude_calls: int = 0
    cursor_runs: int = 0
    last_tool_trace: list[dict] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_session_states: dict[str, SessionState] = {}


def _state_for(session_key: str) -> SessionState:
    """Get-or-create per-session state. `session_key=""` is the global bucket."""
    key = session_key or "__global__"
    if key not in _session_states:
        _session_states[key] = SessionState()
    return _session_states[key]


def _agent_lock_for(session_key: str) -> asyncio.Lock:
    """Per-session asyncio.Lock to serialize agent loops for the same channel."""
    return _state_for(session_key).lock


def has_in_flight_loops() -> bool:
    """True if any session's agent lock is currently held (loop in flight).

    Consulted by the Gemini idle-pause watchdog (`bot._watchdog_task`) so
    it does not tear down the Gemini session while a `do_with_claude`
    loop is running — the loop's eventual narration would otherwise be
    orphaned and the user would see only a 25s idle pause cut into a
    long task.
    """
    return any(s.lock.locked() for s in _session_states.values())


def _humanize_step(tool_name: str, args: dict | None) -> str:
    """One short, user-facing line describing a tool call for the progress spine.

    Deliberately omits sensitive payloads (message bodies, passwords, shell
    commands) — it conveys *what* Aria is doing, never the private *content*.
    """
    a = args or {}
    name = (tool_name or "").lower()
    if tool_name == "create_42c_account":
        u = str(a.get("username", "")).strip()
        label = f" '{u}'" if u else ""
        return f"creating 42c.pw account{label} (deploy ~1-2 min)"
    if "contact" in name:
        who = str(a.get("search") or a.get("name") or "").strip()
        return f"looking up contact{f' {who}' if who else ''}"
    if "message" in name:
        action = str(a.get("action") or "").lower()
        if action == "read":
            who = str(a.get("contact") or a.get("search") or "").strip()
            return f"reading messages{f' with {who}' if who else ''}"
        if action == "create":
            who = str(a.get("to") or a.get("chatId") or "").strip()
            return f"sending iMessage{f' to {who}' if who else ''}"
        return "working with Messages"
    if "calendar" in name:
        return "checking the calendar"
    if any(k in name for k in ("mail", "email", "gmail")):
        return "checking email"
    if any(name.startswith(p) for p in ("execute", "run", "shell")):
        return "running a shell command"
    if "github" in name:
        return "working with GitHub"
    if name.startswith(("read", "get", "list", "search")):
        return f"reading via {tool_name}"
    return f"running {tool_name}"


async def _emit_progress(session_key: str, step: str) -> None:
    """Fire one progress line to the originating channel (and voice).

    Strictly non-fatal: a failed progress post must never break the agent loop.
    """
    if not _progress_callback or not step:
        return
    try:
        await _progress_callback(f"\u2192 {step}", session_key)
    except Exception:
        log.debug("progress emit failed (non-fatal)", exc_info=True)


# Alias-only configuration loaded from projects/registry.md. Maps a short
# registered name to an absolute workspace path. After the unified-cursor-
# agent migration the canonical resolver is `cursor_registry.lookup()`;
# this dict is only consulted to translate registered short names into the
# workspace paths the registry keys on. Code that needs to find an agent
# should call `cursor_registry.lookup(agent_id)` instead of poking this map.
PROJECT_REGISTRY: dict[str, str] = {}


def register_observed_workspace(workspace_root: str | None) -> str | None:
    """Add a basename -> workspace_root entry to PROJECT_REGISTRY if free.

    Called by the cursor external pager whenever a hook event arrives with
    a `workspace_root`. Lets ad-hoc Cursor windows (not in
    projects/registry.md) become addressable by the basename that
    `_classify` puts into `evt.brief`, so a follow-up like
    `read_cursor_window(project="ucs2")` resolves instead of returning
    "Unknown project".

    Returns the registry key used (basename) when an entry was added,
    None when no-op (missing input, empty basename, or a different path
    already owns that basename — registry.md wins).
    """
    if not workspace_root:
        return None
    norm = workspace_root.rstrip("/")
    base = os.path.basename(norm)
    if not base:
        return None
    existing = PROJECT_REGISTRY.get(base)
    if existing is None:
        PROJECT_REGISTRY[base] = norm
        log.info("Auto-registered Cursor workspace: %s -> %s", base, norm)
        return base
    if existing.rstrip("/") != norm:
        log.debug(
            "Workspace basename collision: %s already maps to %s; not overwriting with %s",
            base, existing, norm,
        )
    return None


def init_tools(
    cursor_bridge: CursorBridge,
    post_callback: Callable[..., Coroutine] | None = None,
    alert_callback: Callable[..., Coroutine] | None = None,
    thread_callback: Callable[..., Coroutine] | None = None,
    cursor_event_callback: Callable[..., Coroutine] | None = None,
    reconnect_callback: Callable[..., Coroutine] | None = None,
    discord_history_callback: Callable[..., Coroutine] | None = None,
    discord_threads_callback: Callable[..., Coroutine] | None = None,
    progress_callback: Callable[..., Coroutine] | None = None,
    propose_callback: Callable[..., Coroutine] | None = None,
    ask_callback: Callable[..., Coroutine] | None = None,
) -> None:
    """Initialize tool dependencies. Call once at bot startup."""
    global _anthropic_client, _cursor_bridge
    global _post_callback, _alert_callback, _thread_callback, _cursor_event_callback
    global _reconnect_callback, _progress_callback, _propose_callback, _ask_callback
    global _discord_history_callback, _discord_threads_callback

    # timeout/max_retries bound each agent-loop request (see config note).
    _anthropic_client = anthropic.Anthropic(
        api_key=config.anthropic_api_key,
        timeout=config.anthropic_timeout_sec,
        max_retries=1,
    )
    _cursor_bridge = cursor_bridge
    _post_callback = post_callback
    _alert_callback = alert_callback
    _thread_callback = thread_callback
    _cursor_event_callback = cursor_event_callback
    _reconnect_callback = reconnect_callback
    _progress_callback = progress_callback
    _propose_callback = propose_callback
    _ask_callback = ask_callback
    _discord_history_callback = discord_history_callback
    _discord_threads_callback = discord_threads_callback
    _load_project_registry()

    from . import cursor_tools
    cursor_tools.init_cursor_tools(cursor_bridge)


def set_transcript_provider(provider: Callable[[], list[dict[str, str]]]) -> None:
    """Inject the transcript source. Called by bot.py after Gemini session is created."""
    global _transcript_provider
    _transcript_provider = provider


def set_cancel_flag(value: bool, session_key: str | None = None) -> None:
    """Set the cancel flag for one session, or for all sessions when `session_key` is None.

    `!stop` (emergency-stop) flips all sessions. A per-session cancel
    (e.g. `cancel_current_task` invoked via voice) flips only that one.
    """
    if session_key is None:
        for s in _session_states.values():
            s.cancel = value
    else:
        _state_for(session_key).cancel = value


def _load_project_registry() -> None:
    """Parse projects/registry.md into a name -> path mapping."""
    try:
        with open(config.projects_registry) as f:
            for line in f:
                line = line.strip()
                if line.startswith("-") and "→" in line:
                    name, path = line.lstrip("- ").split("→", 1)
                    PROJECT_REGISTRY[name.strip()] = path.strip()
                elif line.startswith("-") and ":" in line:
                    name, path = line.lstrip("- ").split(":", 1)
                    PROJECT_REGISTRY[name.strip()] = path.strip()
    except FileNotFoundError:
        log.warning("Project registry not found at %s", config.projects_registry)


async def _invoke_handler(
    handler: Callable[..., Coroutine[Any, Any, Any]], args: dict
) -> Any:
    """The one typed dispatch contract for every tool handler.

    `handle_tool_call` and the do_with_claude loop used to splat caller args
    straight into a typed handler (`handler(**args)`), so a single drifted
    argument name crashed the entire call — the root of the
    `_do_with_claude() got an unexpected keyword argument 'prompt'` failure
    (forensic 2026-06-16). Here we pass only the kwargs the handler actually
    accepts: a missing REQUIRED argument returns a clean typed `schema` error
    the model can correct, and a stray unknown argument is dropped with a loud
    log line instead of taking down the call.
    """
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return await handler(**args)  # best effort for un-introspectable callables
    kw_kinds = (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return await handler(**args)
    accepted = {n for n, p in sig.parameters.items() if p.kind in kw_kinds}
    required = {
        n for n, p in sig.parameters.items()
        if p.kind in kw_kinds and p.default is inspect.Parameter.empty
    }
    filtered = {k: v for k, v in args.items() if k in accepted}
    missing = required - filtered.keys()
    if missing:
        return json.dumps({
            "_error_class": "schema",
            "error": (
                f"argument mismatch: missing required {sorted(missing)}. "
                f"Provided {sorted(args)}; this tool accepts {sorted(accepted)}."
            ),
        })
    dropped = args.keys() - accepted
    if dropped:
        log.warning(
            "dispatch dropped unknown args for %s: %s",
            getattr(handler, "__name__", repr(handler)), sorted(dropped),
        )
    return await handler(**filtered)


async def handle_tool_call(name: str, args: dict) -> str:
    """Dispatch a tool call by name. Returns JSON string."""
    handlers = {
        "plan_with_claude": _plan_with_claude,
        "package_audit_findings": _package_audit_findings,
        "build_with_cursor": _build_with_cursor,
        "query_cursor": _query_cursor,
        "cursor_status": _cursor_status_new,
        "do_with_claude": _do_with_claude,
        # Durable, backgroundable Tasks (Primitive 1): start one and walk away;
        # read it back later from the Task object, not the chat.
        "start_task": _start_task,
        "task_status": _task_status,
        # Playbooks: an ordered list of Tasks — name it, walk away.
        "run_playbook": _run_playbook,
        "list_playbooks": _list_playbooks,
        "create_42c_account": _create_42c_account,
        "propose_action": _propose_action,
        "ask_user": _ask_user_tool,
        "remember": _remember,
        "recall": _recall,
        "cancel_current_task": _cancel_current_task,
        "confirm_action": _confirm_action_noop,
        "quick_email_check": _quick_email_check,
        "quick_calendar": _quick_calendar,
        "list_prompts": _list_prompts,
        "show_prompt": _show_prompt,
        "edit_prompt": _edit_prompt,
        "rollback_prompt": _rollback_prompt,
        "prompt_versions": _prompt_versions,
        "reload_prompts": _reload_prompts,
        "get_focused_app": _get_focused_app,
        "focus_app": _focus_app,
        "dictate_into_focused_app": _dictate_into_focused_app,
        # Unified cursor tool surface — the six replacements. The per-window
        # handlers below (_list_cursor_windows, _read_cursor_window,
        # _send_to_cursor_chat, _screenshot_cursor_window, …) still live in
        # this file as private helpers used by `cursor_tools` for IDE-side
        # osascript fallback, but they are no longer dispatched by name.
        "cursor_agents": _cursor_agents,
        "cursor_read": _cursor_read,
        "cursor_send": _cursor_send,
        "cursor_spawn": _cursor_spawn,
        "cursor_screenshot": _cursor_screenshot,
        "cursor_threads": _cursor_threads,
        # Claude Code (Agent SDK) — Aria drives Claude Code on a repo.
        "claude_code_spawn": _claude_code_spawn,
        "claude_code_send": _claude_code_send,
        "claude_code_read": _claude_code_read,
        "claude_code_threads": _claude_code_threads,
        # Discord text-history tools — read-only.
        "discord_recent_messages": _discord_recent_messages,
        "discord_list_threads": _discord_list_threads,
        # DGX Spark control (shared with the CLI harness via src/spark.py).
        "spark_status": _spark_status,
        "spark_verify": _spark_verify,
        "spark_setup": _spark_setup,
        # modelvault cold-backup (launches a cloud VM; sibling repo).
        "backup_model": _backup_model,
        # DGX Spark Claude Code workspace + headless audit/collapse runs.
        "spark_cc_sync": _spark_cc_sync,
        "spark_cc_auth": _spark_cc_auth,
        "spark_run": _spark_run,
        "spark_run_status": _spark_run_status,
        "spark_run_fetch": _spark_run_fetch,
    }
    handler = handlers.get(name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {name}"})

    spend = get_daily_spend()
    _free_tools = (
        "cursor_status", "recall", "cancel_current_task", "list_prompts",
        "show_prompt", "prompt_versions", "reload_prompts",
        "get_focused_app", "focus_app", "dictate_into_focused_app",
        "cursor_agents", "cursor_read", "cursor_send", "cursor_spawn",
        "cursor_screenshot",
        # Claude Code runs on the Max subscription (Agent SDK credit), bounded
        # per-run by claude_code_max_budget_usd — not the real-API daily $ cap.
        "claude_code_spawn", "claude_code_send", "claude_code_read", "claude_code_threads",
        "discord_recent_messages", "discord_list_threads",
        # No paid API spend — just an htpasswd write + Fly deploy. Must remain
        # usable even when the daily ceiling is hit (it's a user-requested action).
        "create_42c_account",
        # Just posts a card + spawns a waiter; the approved task pays its own
        # spend when it runs. Proposing is free.
        "propose_action",
        # Posts a question + blocks for a reply; spends nothing itself.
        "ask_user",
        # Starting a Task just persists a row + spawns a background runner (which
        # pays its own spend when it runs); reading a Task is free. Must stay
        # usable at the cap so "start this and walk away" / "how's X?" always work.
        "start_task", "task_status",
        # A playbook just spawns a background sequence of Tasks (each pays its own
        # spend); listing is free.
        "run_playbook", "list_playbooks",
        # Spark ops/diagnostics must stay usable even at the daily cap: status
        # is read-only, setup spends nothing on our API, and verify only spends
        # a few cents on Gemini visual checks. Operability beats the gate here.
        "spark_status", "spark_verify", "spark_setup",
        # Backup spend is GCP (the VM + storage), not our Anthropic daily cap; the
        # launch itself costs nothing on our API. Must stay usable at the cap.
        "backup_model",
        # Spark Claude Code runs bill the Max subscription (not our real-API
        # daily $ cap), and sync/auth/status/fetch spend nothing on our API.
        "spark_cc_sync", "spark_cc_auth", "spark_run", "spark_run_status", "spark_run_fetch",
    )
    if spend >= config.daily_spend_cap_usd and name not in _free_tools:
        return json.dumps({"error": f"Daily spend cap (${config.daily_spend_cap_usd}) reached. Current: ${spend:.2f}"})

    transcript = _transcript_provider() if _transcript_provider else []
    session_key = args.get("session_key", "")
    state = _state_for(session_key)
    state.last_tool_trace = None

    start = time.monotonic()
    try:
        result = await _invoke_handler(handler, args)
        duration_ms = int((time.monotonic() - start) * 1000)
        log_event(name, args, str(result)[:10_000], duration_ms)
        result_str = result if isinstance(result, str) else json.dumps(result)

        trace = state.last_tool_trace
        state.last_tool_trace = None
        _emit_session_record(
            name, args, session_key, transcript,
            result_str, duration_ms, status="ok",
            tool_trace=trace,
        )
        return result_str
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        log.exception("Tool call %s failed", name)
        error_result = json.dumps({"error": str(e)})
        trace = state.last_tool_trace
        state.last_tool_trace = None
        _emit_session_record(
            name, args, session_key, transcript,
            error_result, duration_ms, status="error",
            tool_trace=trace,
        )
        return error_result


def _emit_session_record(
    tool_name: str,
    args: dict,
    session_key: str,
    transcript: list[dict[str, str]],
    result: str,
    duration_ms: int,
    status: str,
    tool_trace: list[dict] | None = None,
) -> None:
    """Write a session record and optionally fire the judge (EMIT layer).

    tool_trace, when provided, is a list of MCP tool calls that happened
    during the agent loop. Each entry has {tool, args_summary, result_preview}.
    This goes into context_json so the judge can verify spec properties like
    'Tool execution required' and 'No fabricated results'.
    """
    product = tool_to_product(tool_name)
    if product == "system":
        return

    inputs = {"args": args, "transcript": transcript}
    outputs = {"result": result[:1_000_000], "duration_ms": duration_ms, "status": status}
    context = {"tool_trace": tool_trace} if tool_trace else None

    # EMIT only — the record is judged durably by the periodic sweep in
    # src/judge.py (sweep_unjudged, scheduled from bot.on_ready). The previous
    # inline `asyncio.create_task(...)` was a fire-and-forget orphan dropped on
    # process churn, so the longest/failed sessions went unjudged (~45%). The
    # sweep finds every record that still lacks a verdict via a DB LEFT JOIN,
    # so judging now survives restarts with exactly one path.
    record_session(
        session_key=session_key or "unknown",
        tool_name=tool_name,
        inputs=inputs,
        outputs=outputs,
        context=context,
    )


# ---------------------------------------------------------------------------
# Plan with Claude
# ---------------------------------------------------------------------------

async def _plan_with_claude(
    context: str,
    session_key: str,
    prompt_template: str = "planning",
) -> str:
    result = await _plan_with_claude_legacy(context, session_key, prompt_template)

    # Ground write at the seam that knows the artifact: this plan is now what
    # "the plan" refers to. Telemetry-class — never breaks the planning path.
    try:
        if result and not result.lstrip().startswith('{"error"'):
            first_line = next(
                (ln.strip().lstrip("#* ").strip()
                 for ln in result.splitlines() if ln.strip()),
                "",
            )
            set_ground(
                "active_plan",
                label=first_line[:120] or f"plan for: {context[:100]}",
                detail=(
                    f"latest plan from planning thread {session_key}; full "
                    "text in that Discord thread / planning_history"
                ),
                source=session_key,
            )
    except Exception:
        log.warning("ground write (active_plan) failed", exc_info=True)
    return result


async def _plan_with_claude_legacy(
    context: str,
    session_key: str,
    prompt_template: str = "planning",
) -> str:
    state = _state_for(session_key)
    state.cancel = False
    state.claude_calls += 1
    if state.claude_calls > config.per_session_claude_calls_max:
        return json.dumps({"error": f"Per-session Claude call limit ({config.per_session_claude_calls_max}) reached"})
    if not _anthropic_client:
        return json.dumps({"error": "Anthropic client not initialized"})

    template = load_template(prompt_template)
    history = get_planning_history(session_key)

    memories = mem_recall(context, limit=3)
    if memories:
        memory_context = "\n".join(f"- {m.get('memory', m.get('text', ''))}" for m in memories)
        context = f"Relevant memories:\n{memory_context}\n\n{context}"

    messages = history + [{"role": "user", "content": context}]

    started_at = time.monotonic()
    started_at_iso = _now_iso()

    response = await asyncio.to_thread(
        _anthropic_client.messages.create,
        model=config.claude_model,
        system=template,
        messages=messages,
        max_tokens=8192,
    )

    latency_ms = int((time.monotonic() - started_at) * 1000)
    result_text = response.content[0].text
    append_planning_history(session_key, "user", context)
    append_planning_history(session_key, "assistant", result_text)

    cost = 0.0
    if response.usage:
        cost = _estimate_cost(response.usage.input_tokens, response.usage.output_tokens)
        log_event("plan_with_claude", {"session_key": session_key}, result_text[:200], 0, session_key, cost)

    log_loop_execution(
        tool_name="plan_with_claude",
        session_key=session_key,
        prompt_template=prompt_template,
        model_id=config.claude_model,
        tokens_in=response.usage.input_tokens if response.usage else None,
        tokens_out=response.usage.output_tokens if response.usage else None,
        cost_usd=cost,
        latency_ms=latency_ms,
        iterations=1,
        status="completed",
        started_at=started_at_iso,
    )

    return result_text


# ---------------------------------------------------------------------------
# Package audit findings
# ---------------------------------------------------------------------------

AUDIT_FINDINGS_FILENAME = "audit_findings.md"


async def _package_audit_findings(
    agent_id: str,
    scope_hint: str = "",
    n_recent_turns: int = 20,
    session_key: str = "",
) -> str:
    """Synthesize the recent voice dialogue into structured audit findings.

    The audit-review flow has two distinct moments. (1) Corbin and Aria
    have a normal dialogue while he watches Cursor's visible audit run.
    (2) When he says "package that up," Aria calls this tool — once —
    and Claude turns the dialogue into structured findings appended to
    `<workspace_root>/audit_findings.md`. Nothing is sent to Cursor here;
    that is a separate `cursor_send` (or `propose_action`) on Corbin's
    explicit yes.

    Reads:
    - last `n_recent_turns` from the shared `conversation` buffer.
    - existing `<workspace_root>/audit_findings.md` (Cursor's first pass).

    Writes:
    - appends a `# Human review — <ts>` block to `audit_findings.md`.
    - posts a plain-English summary to `#ucs` via the post callback.
    """
    if not _anthropic_client:
        return json.dumps({"error": "Anthropic client not initialized"})

    agent = cursor_registry.lookup(agent_id)
    if agent is None:
        return json.dumps({
            "error": (
                f"Unknown agent_id: {agent_id!r}. "
                "Call cursor_agents to list current agents."
            ),
        })

    workspace_root = agent.workspace_root
    if not workspace_root or not os.path.isdir(workspace_root):
        return json.dumps({
            "error": (
                f"agent.workspace_root is not a directory on disk: "
                f"{workspace_root!r}"
            ),
        })

    findings_path = os.path.join(workspace_root, AUDIT_FINDINGS_FILENAME)
    existing_findings = ""
    if os.path.exists(findings_path):
        with open(findings_path) as f:
            existing_findings = f.read()

    recent = conversation.recent(max_turns=max(1, int(n_recent_turns)))
    if not recent:
        return json.dumps({
            "error": (
                "No recent conversation turns to package. Talk through the "
                "findings with Aria first, then ask her to package them."
            ),
        })

    dialogue_lines: list[str] = []
    for t in recent:
        if t.role == "user":
            speaker = "Corbin"
        elif t.role == "aria":
            speaker = "Aria"
        else:
            continue
        dialogue_lines.append(f"{speaker} ({t.medium}): {t.short()}")

    if not dialogue_lines:
        return json.dumps({
            "error": (
                "Recent turns contain no user or Aria speech — nothing to "
                "package."
            ),
        })

    sections: list[str] = []
    if scope_hint.strip():
        sections.append(f"## Scope hint from Corbin\n{scope_hint.strip()}")
    sections.append(
        "## Recent dialogue (oldest first)\n" + "\n".join(dialogue_lines)
    )
    sections.append(
        "## Existing audit_findings.md\n"
        + (existing_findings.strip() or
           "(empty — Cursor has not written a first pass yet)")
    )
    context = "\n\n".join(sections)

    template = load_template("audit_packaging")

    started_at = time.monotonic()
    started_at_iso = _now_iso()
    response = await asyncio.to_thread(
        _anthropic_client.messages.create,
        model=config.claude_model,
        system=template,
        messages=[{"role": "user", "content": context}],
        max_tokens=4096,
    )
    latency_ms = int((time.monotonic() - started_at) * 1000)
    result_text = response.content[0].text.strip()

    cost = 0.0
    if response.usage:
        cost = _estimate_cost(
            response.usage.input_tokens, response.usage.output_tokens
        )
    log_event(
        "package_audit_findings",
        {
            "agent_id": agent_id,
            "scope_hint": scope_hint,
            "n_recent_turns": n_recent_turns,
            "session_key": session_key,
        },
        result_text[:200],
        latency_ms,
        session_key,
        cost,
    )
    log_loop_execution(
        tool_name="package_audit_findings",
        session_key=session_key,
        prompt_template="audit_packaging",
        model_id=config.claude_model,
        tokens_in=response.usage.input_tokens if response.usage else None,
        tokens_out=response.usage.output_tokens if response.usage else None,
        cost_usd=cost,
        latency_ms=latency_ms,
        iterations=1,
        status="completed",
        started_at=started_at_iso,
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    new_block = f"\n\n# Human review — {timestamp}\n\n{result_text}\n"
    with open(findings_path, "a") as f:
        f.write(new_block)

    finding_titles = [
        ln[3:].strip()
        for ln in result_text.splitlines()
        if ln.startswith("## ") and not ln.startswith("## Dispatch note")
    ]

    project_label = agent.project_label or os.path.basename(
        workspace_root.rstrip("/")
    )
    post_body = (
        f"**Audit review packaged — {len(finding_titles)} finding"
        f"{'s' if len(finding_titles) != 1 else ''}**\n"
        f"Project: `{project_label}`. File: `{findings_path}`.\n"
        f"Next: tell Aria \"send to Cursor\" to dispatch, or talk through "
        f"changes first.\n\n"
        f"{result_text}"
    )
    if _post_callback:
        await _post_callback(post_body)

    return json.dumps({
        "ok": True,
        "agent_id": agent.agent_id,
        "workspace_root": workspace_root,
        "findings_path": findings_path,
        "finding_count": len(finding_titles),
        "finding_titles": finding_titles,
    })


# ---------------------------------------------------------------------------
# Build with Cursor
# ---------------------------------------------------------------------------

async def _build_with_cursor(
    project: str,
    instruction: str,
    background: bool = True,
    session_key: str = "",
) -> dict[str, Any]:
    state = _state_for(session_key)
    state.cursor_runs += 1
    if state.cursor_runs > config.per_session_cursor_runs_max:
        return {"error": f"Per-session Cursor run limit ({config.per_session_cursor_runs_max}) reached"}
    if not _cursor_bridge:
        return {"error": "Cursor bridge not initialized"}

    project_path = PROJECT_REGISTRY.get(project)
    if not project_path:
        return {"error": f"Unknown project: {project}. Known: {list(PROJECT_REGISTRY.keys())}"}

    impl_prompt = load_template("implementation")
    full_instruction = f"{impl_prompt}\n\n---\n\n{instruction}"

    session_id = await _cursor_bridge.create_session(project_path, full_instruction)
    upsert_cursor_session(session_id, project)

    # Ground write: this project is now what "the project" refers to.
    try:
        set_ground(
            "active_project",
            label=project,
            path=project_path,
            detail=f"cursor build session {session_id[:8]}",
            source=session_key or "build_with_cursor",
        )
    except Exception:
        log.warning("ground write (active_project) failed", exc_info=True)

    thread = None
    if _thread_callback:
        thread = await _thread_callback(session_id, project)
        if thread:
            await thread.send(f"Build started for **{project}** (session `{session_id[:8]}`)")

    if _cursor_event_callback:
        asyncio.create_task(_cursor_event_callback(session_id, thread))

    return {"session_id": session_id, "initial_status": "running"}


# ---------------------------------------------------------------------------
# Query / Status Cursor
# ---------------------------------------------------------------------------

async def _query_cursor(session_id: str, message: str) -> dict[str, Any]:
    if not _cursor_bridge:
        return {"error": "Cursor bridge not initialized"}
    result = await _cursor_bridge.send_message(session_id, message)
    return {"ok": True, **result}


async def _cursor_status() -> list[dict[str, Any]]:
    return get_active_cursor_sessions()


# ---------------------------------------------------------------------------
# Discord text-history tools
# ---------------------------------------------------------------------------
#
# Aria's window into Discord channel and thread history. The fetchers are
# injected by bot.py at init time so this layer has no hard dependency on
# the py-cord client. Use cases:
#
# - "What did Cursor just post in the foo build thread?"
#     -> discord_list_threads("ucs") to find the build thread,
#        discord_recent_messages("Build: foo (sid)") to read it.
# - "Catch me up on #ucs while I was away."
#     -> discord_recent_messages("ucs", limit=30).
# - "Did anything land in #ucs-alerts overnight?"
#     -> discord_recent_messages("alerts", limit=50).

async def _discord_recent_messages(channel: str = "ucs", limit: int = 20) -> str:
    """Return the most recent messages from a Discord text channel or thread.

    `channel` accepts a numeric id, a `<#id>` mention, a channel name
    (with or without leading `#`), an alias (`ucs`, `alerts`,
    `spicy-lit`), or a thread name (substring match across active
    threads). `limit` is clamped to [1, 100]. Messages come back
    oldest-first so the caller can read them top-to-bottom.
    """
    if _discord_history_callback is None:
        return json.dumps({"error": "Discord history callback not wired."})
    try:
        n = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        n = 20
    try:
        msgs = await _discord_history_callback(channel, n)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        log.exception("discord_recent_messages failed for %r", channel)
        return json.dumps({"error": f"history fetch failed: {exc}"})
    return json.dumps(
        {
            "channel": channel,
            "count": len(msgs),
            "messages": msgs,
        }
    )


async def _discord_list_threads(channel: str = "ucs") -> str:
    """List active threads under a Discord channel.

    Use to discover build threads (created by Aria's cursor_spawn
    pipeline) and any other threads under `#ucs` or `#ucs-alerts`. The
    response includes thread ids and names you can pass back to
    `discord_recent_messages` to read history.
    """
    if _discord_threads_callback is None:
        return json.dumps({"error": "Discord threads callback not wired."})
    try:
        threads = await _discord_threads_callback(channel)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        log.exception("discord_list_threads failed for %r", channel)
        return json.dumps({"error": f"threads fetch failed: {exc}"})
    return json.dumps(
        {
            "channel": channel,
            "count": len(threads),
            "threads": threads,
        }
    )


# ---------------------------------------------------------------------------
# Do with Claude (MCP agent loop)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 42c.pw account provisioning
#
# 42c.pw is gated by a single shared HTTP Basic Auth file (c42_public/.htpasswd)
# baked into the alive-river Fly image. "Creating an account" therefore means
# upserting a `username:apr1hash` line and redeploying so the new login goes
# live. This one deterministic tool replaces the fuzzy htpasswd/openssl shell
# improvisation that caused the historical 42c.pw failure.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Cursor thread distillation (cheap-model summaries for the thread roster)
# ---------------------------------------------------------------------------

# claude-haiku per-million-token rates (models.yaml::claude-haiku). The
# distiller runs on config.distill_model — a Haiku — so spend is tracked at
# Haiku rates, not Opus. If distill_model is repointed at a pricier model,
# update these to match so the daily cap stays honest.
_DISTILL_COST_PER_M_IN = 0.25
_DISTILL_COST_PER_M_OUT = 1.25


def _parse_distill_json(text: str) -> dict:
    """Extract the JSON object a distill turn returned. Loud on failure."""
    if not text:
        raise ValueError("distiller returned empty text")
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"distiller returned no JSON object: {text[:160]!r}")
    return json.loads(text[start : end + 1])


async def _distill_thread(*, project_label: str, digest: dict) -> tuple[dict, float]:
    """Distill one thread digest into a card via the cheap model. Raises loud.

    Returns (card, cost_usd). `card` has label/purpose/did/status/open_question.
    No silent fallback: a missing client or unparseable output raises so the
    caller surfaces the failure on that thread's row rather than hiding it.
    """
    if not _anthropic_client:
        raise RuntimeError("Anthropic client not initialized — cannot distill thread")
    template = load_template("distill_thread")
    payload = {
        "project": project_label,
        "intent": digest.get("first_user_text", ""),
        "turns": digest.get("turns", 0),
        "recent_assistant_turns": digest.get("recent_assistant_texts", []),
    }
    started = time.monotonic()
    started_iso = _now_iso()
    resp = await asyncio.to_thread(
        _anthropic_client.messages.create,
        model=config.distill_model,
        system=template,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        max_tokens=500,
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    text = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    cost = 0.0
    if resp.usage:
        cost = (
            resp.usage.input_tokens / 1_000_000 * _DISTILL_COST_PER_M_IN
            + resp.usage.output_tokens / 1_000_000 * _DISTILL_COST_PER_M_OUT
        )
    log_loop_execution(
        tool_name="distill_thread",
        session_key="",
        prompt_template="distill_thread",
        model_id=config.distill_model,
        tokens_in=resp.usage.input_tokens if resp.usage else None,
        tokens_out=resp.usage.output_tokens if resp.usage else None,
        cost_usd=cost,
        latency_ms=latency_ms,
        iterations=1,
        status="completed",
        started_at=started_iso,
    )
    # Record spend in events too so the daily cap accounts for distillation.
    log_event("distill_thread", {"project": project_label}, text[:200], latency_ms, "", cost)
    card = _parse_distill_json(text)
    return card, cost


_CREATE_42C_TOOL_SCHEMA: dict[str, Any] = {
    "name": "create_42c_account",
    "description": (
        "Create a login account on the 42c.pw website so a person can access "
        "what Corbin is working on. 42c.pw uses shared HTTP Basic Auth; this "
        "adds the username/password credential and redeploys so it goes live "
        "(takes ~1-2 minutes). Returns the login URL plus the username and "
        "password to share. Use whenever the user asks to make/create an "
        "account for someone."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "username": {"type": "string", "description": "The account username/login (a short handle)."},
            "password": {"type": "string", "description": "The account password."},
            "label": {"type": "string", "description": "Optional note about who the account is for, e.g. the person's name."},
        },
        "required": ["username", "password"],
    },
}


# Cursor-thread tools, mirrored into the text (do_with_claude) agent loop so a
# "#ucs" message gets the same durable, per-thread answers Aria gives in voice.
_CURSOR_THREADS_TOOL_SCHEMA: dict[str, Any] = {
    "name": "cursor_threads",
    "description": (
        "List the recent Cursor coding threads in a project (default "
        "live_visuals_4), each distilled into a plain-English card: a short "
        "label, what it set out to do, what it actually did, status, and any "
        "open question. THIS is how you answer 'what's going on in <project>?' "
        "or 'what is each thread?' — call it instead of guessing from watch "
        "events. Threads are the user's parallel Cursor agents; their real "
        "names are UUIDs, so read back the distilled labels."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "Project name (default 'live_visuals_4'), a registry alias, or an absolute workspace path."},
            "window_hours": {"type": "number", "description": "Only threads active within this many hours (default 48)."},
            "limit": {"type": "integer", "description": "Max threads to return, newest first (default 12)."},
            "refresh": {"type": "boolean", "description": "Re-distill even cached threads (default false). Use only if asked for a fresh read."},
        },
        "required": [],
    },
}

_CURSOR_READ_TOOL_SCHEMA: dict[str, Any] = {
    "name": "cursor_read",
    "description": (
        "Read the recent transcript turns of a specific Cursor thread to dig "
        "deeper after cursor_threads. Pass the thread handle as "
        "'<project>/<sid_prefix>' (e.g. 'live_visuals_4/57480d46') to target "
        "one exact thread, or a project/agent handle for its current session."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Thread/agent handle, e.g. 'live_visuals_4/57480d46' or a workspace path."},
            "n_turns": {"type": "integer", "description": "How many recent turns to return (default 5, max 25)."},
            "sid": {"type": "string", "description": "Optional explicit transcript sid (full or prefix) if not encoded in agent_id."},
        },
        "required": ["agent_id"],
    },
}

_CURSOR_SEND_TOOL_SCHEMA: dict[str, Any] = {
    "name": "cursor_send",
    "description": (
        "Send a message/instruction to a Cursor thread (chat), or approve / "
        "reject / cancel it. Use to act across threads after cursor_threads. "
        "SDK-spawned threads accept any kind; an IDE thread routes to its "
        "focused window."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Thread/agent handle from cursor_threads/cursor_agents."},
            "message": {"type": "string", "description": "The message to send (for kind=chat/new_agent/cancel)."},
            "kind": {"type": "string", "description": "chat | new_agent | approve | reject | cancel. Default chat."},
            "note": {"type": "string", "description": "Optional note appended to approve/reject."},
        },
        "required": ["agent_id"],
    },
}

_CURSOR_SPAWN_TOOL_SCHEMA: dict[str, Any] = {
    "name": "cursor_spawn",
    "description": (
        "Start a NEW Cursor coding thread (a fresh Claude-backed agent) in a "
        "project and hand it an instruction. THIS is how you act on 'put this "
        "in its own thread', 'spin up a new thread for X', or 'send it to a "
        "new thread' — do it, don't explain that you can't. Returns the "
        "agent_id handle; follow up with cursor_read (to see what it did) or "
        "cursor_send (to steer it). For an existing project pass its name as "
        "workspace_root (e.g. 'live_visuals_4'); it resolves via the registry."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "workspace_root": {"type": "string", "description": "Project name/alias (e.g. 'live_visuals_4') or an absolute workspace path to spawn the thread in."},
            "instruction": {"type": "string", "description": "The full task/instruction the new thread should carry out."},
            "model": {"type": "string", "description": "Optional model override; omit to use the configured default."},
        },
        "required": ["workspace_root", "instruction"],
    },
}

_CURSOR_AGENTS_TOOL_SCHEMA: dict[str, Any] = {
    "name": "cursor_agents",
    "description": (
        "List every live Cursor agent/thread the system can see right now "
        "(IDE windows the user opened and threads you spawned), with each "
        "one's agent_id handle, status, and last message. Use it to find the "
        "handle for a thread before cursor_read / cursor_send / cursor_spawn."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

_CLAUDE_CODE_SPAWN_TOOL_SCHEMA: dict[str, Any] = {
    "name": "claude_code_spawn",
    "description": (
        "Start a NEW Claude Code thread on a repo and hand it an instruction. "
        "This is how Aria drives Claude Code (the migrated live_visuals_4_CC by "
        "default). Defaults to Plan Mode, so it proposes a plan to review/edit "
        "before any file changes. Returns session_id; then claude_code_read to "
        "see the plan and claude_code_send (kind=approve) to execute it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "workspace_root": {"type": "string", "description": "Project name or absolute path. Omit for the managed live_visuals_4_CC repo."},
            "instruction": {"type": "string", "description": "The task/instruction for the Claude Code thread."},
            "mode": {"type": "string", "description": "plan (default) | acceptEdits | default. Plan first unless the user said to just do it."},
        },
        "required": ["instruction"],
    },
}

_CLAUDE_CODE_SEND_TOOL_SCHEMA: dict[str, Any] = {
    "name": "claude_code_send",
    "description": (
        "Send a follow-up to a live Claude Code thread, or approve / reject / "
        "cancel it. kind=approve proceeds with the plan (switches to acceptEdits "
        "so it executes); kind=chat sends a message; kind=cancel tears it down. "
        "Get the agent_id from cursor_agents / claude_code_threads."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Thread handle (workspace path/name) from claude_code_threads/cursor_agents."},
            "message": {"type": "string", "description": "The message (for kind=chat), or note for approve/reject."},
            "kind": {"type": "string", "description": "chat | approve | reject | cancel. Default chat."},
        },
        "required": ["agent_id"],
    },
}

_CLAUDE_CODE_READ_TOOL_SCHEMA: dict[str, Any] = {
    "name": "claude_code_read",
    "description": (
        "Read the latest turns + status of a Claude Code thread (its proposed "
        "plan, progress, or pending question). Omit agent_id for the managed "
        "live_visuals_4_CC thread."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Thread handle; omit for the managed repo."},
            "n_turns": {"type": "integer", "description": "Recent turns to return (default 5, max 25)."},
        },
        "required": [],
    },
}

_CLAUDE_CODE_THREADS_TOOL_SCHEMA: dict[str, Any] = {
    "name": "claude_code_threads",
    "description": (
        "List the Claude Code threads Aria is driving, with status and pending "
        "questions. Use to find a thread handle before claude_code_read / send."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "Optional project filter; omit for all."},
        },
        "required": [],
    },
}


def _apr1_hash(password: str) -> str:
    """Apache apr1 (MD5) password hash via openssl — the format nginx expects.

    Loud failure: a non-zero openssl exit raises so the caller can surface it.
    """
    proc = subprocess.run(
        ["openssl", "passwd", "-apr1", password],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"openssl apr1 failed: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def _upsert_htpasswd(htpasswd_path: str, username: str, hashed: str) -> None:
    """Add or replace the `username:hash` line (durable, idempotent)."""
    lines: list[str] = []
    if os.path.exists(htpasswd_path):
        with open(htpasswd_path) as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    lines = [ln for ln in lines if ln.split(":", 1)[0] != username]
    lines.append(f"{username}:{hashed}")
    with open(htpasswd_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def _verify_42c_login(url: str, username: str, password: str) -> bool:
    """Best-effort: do these Basic-Auth creds get a 2xx/3xx from the live site?"""
    try:
        proc = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
             "-u", f"{username}:{password}", url],
            capture_output=True, text=True, timeout=30,
        )
        code = (proc.stdout or "").strip()
        return code.startswith(("2", "3"))
    except Exception:
        log.debug("42c verify curl failed (non-fatal)", exc_info=True)
        return False


async def _create_42c_account(
    username: str,
    password: str,
    label: str = "",
    deploy: bool = True,
    session_key: str = "",
) -> str:
    """Provision a 42c.pw Basic-Auth account: hash -> upsert -> deploy -> verify."""
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return json.dumps({"error": "username and password are required"})
    if ":" in username or any(c.isspace() for c in username):
        return json.dumps({"error": "username must not contain ':' or whitespace"})

    c42_dir = config.c42_public_dir
    # The credential's single home is c42_public/.htpasswd; the canonical deploy
    # script that ships it lives one level up at the repo root (it bakes
    # c42_public into the alive-river Fly image). c42_public has no deploy.sh of
    # its own.
    repo_root = os.path.dirname(c42_dir)
    htpasswd_path = os.path.join(c42_dir, ".htpasswd")
    deploy_script = os.path.join(repo_root, "deploy.sh")
    if not os.path.isdir(c42_dir) or not os.path.exists(deploy_script):
        return json.dumps({"error": f"42c.pw deploy script not found at {deploy_script}"})

    try:
        hashed = await asyncio.to_thread(_apr1_hash, password)
        await asyncio.to_thread(_upsert_htpasswd, htpasswd_path, username, hashed)
    except Exception as e:
        log.exception("create_42c_account: hash/upsert failed")
        return json.dumps({"error": f"failed to write credential: {e}"})

    url = config.c42_url
    if not deploy:
        return json.dumps({
            "ok": True, "deployed": False, "url": url,
            "username": username, "password": password,
            "note": "credential staged in .htpasswd but not deployed — not live yet.",
        })

    await _emit_progress(session_key, "deploying 42c.pw (~1-2 min)")
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["bash", deploy_script],
            capture_output=True, text=True,
            timeout=config.c42_deploy_timeout_sec, cwd=repo_root,
        )
    except Exception as e:
        log.exception("create_42c_account: deploy failed to run")
        return json.dumps({"error": f"deploy failed to start: {e}", "username": username})
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-600:]
        log.error("create_42c_account: deploy.sh exit %d: %s", proc.returncode, tail)
        return json.dumps({
            "error": f"deploy failed (exit {proc.returncode})",
            "detail": tail, "username": username,
        })

    verified = await asyncio.to_thread(_verify_42c_login, url, username, password)
    log.info("create_42c_account: provisioned %s (verified=%s)", username, verified)
    return json.dumps({
        "ok": True, "deployed": True, "verified": verified,
        "url": url, "username": username, "password": password, "label": label,
        "share_text": f"Login at {url} — username: {username}, password: {password}",
    })


# ---------------------------------------------------------------------------
# DGX Spark — Aria's first-class handle on the two GB10 nodes (spark1, spark2)
# ---------------------------------------------------------------------------
# These reuse the shared catalog in src/spark.py — the CLI acceptance harness
# (scripts/spark_acceptance.py) calls the exact same code, so there is one
# implementation, not two. status() is read-only and free; verify() runs the
# full "prove it twice" capture+Gemini acceptance; setup() is executable (seeds
# the node via setup_node.sh). The blocking spark.* calls run in a worker thread
# so the event loop is never stalled. See ops/spark/NODES.md.

def _spark_known_nodes() -> str:
    return ", ".join(spark.NODES)


async def _spark_status(node: str = "", session_key: str = "") -> str:
    """Read-only health for one spark node, or BOTH when node is empty/'all'."""
    target = (node or "").strip().lower()
    try:
        if target in ("", "all", "both", "*"):
            names = list(spark.NODES)
            reports = await asyncio.gather(
                *(asyncio.to_thread(spark.status, n) for n in names)
            )
            return json.dumps({"ok": all(r.get("ok") for r in reports), "nodes": list(reports)})
        if target not in spark.NODES:
            return json.dumps({"error": f"unknown node {node!r}; known: {_spark_known_nodes()}"})
        return json.dumps(await asyncio.to_thread(spark.status, target))
    except Exception as e:
        log.exception("spark_status failed")
        return json.dumps({"error": f"spark_status failed: {e}"})


async def _spark_verify(node: str = "", role: str = "", only: str = "", session_key: str = "") -> str:
    """Full Section-A acceptance on ONE node: machine assertion AND Gemini agree."""
    target = (node or "").strip().lower()
    if target not in spark.NODES:
        return json.dumps({"error": f"node is required; known: {_spark_known_nodes()}"})
    only_list = [g.strip() for g in (only or "").split(",") if g.strip()] or None
    await _emit_progress(
        session_key,
        f"verifying {target} (opens Terminal windows + independent Gemini checks; ~a few minutes)",
    )
    try:
        report = await asyncio.to_thread(spark.verify, target, role or None, only_list)
    except Exception as e:
        log.exception("spark_verify failed")
        return json.dumps({"error": f"spark_verify failed: {e}"})

    summary = report["summary"]
    failed = [g for g in report["gates"] if g["verdict"] != "PASS"]
    if _post_callback:
        lines = [
            f"**Spark acceptance — {report['node']} (role {report['role']}): "
            f"{summary['pass']}/{summary['total']} green**"
        ]
        for g in report["gates"]:
            mark = "\u2705" if g["verdict"] == "PASS" else "\u274c"
            lines.append(f"{mark} `{g['id']}` — {g['title']}")
            if g["verdict"] != "PASS":
                lines.append(f"    machine: {g['machine_detail']}")
                lines.append(f"    gemini : {g['gemini_reason']}")
                lines.append(f"    fix    : {g['fix']}")
        lines.append(f"_artifacts: data/spark/{report['node']}/ (one PNG per gate + acceptance.json)_")
        try:
            await _post_callback("\n".join(lines))
        except Exception:
            log.debug("spark_verify channel post failed (non-fatal)", exc_info=True)
    return json.dumps({"ok": not failed, **report})


async def _spark_setup(node: str = "", role: str = "", session_key: str = "") -> str:
    """EXECUTABLE: run ops/spark/setup_node.sh on the node (idempotent)."""
    target = (node or "").strip().lower()
    if target not in spark.NODES:
        return json.dumps({"error": f"node is required; known: {_spark_known_nodes()}"})
    await _emit_progress(session_key, f"seeding {target} via setup_node.sh (~1-3 min)")
    try:
        return json.dumps(await asyncio.to_thread(spark.setup, target, role or ""))
    except Exception as e:
        log.exception("spark_setup failed")
        return json.dumps({"error": f"spark_setup failed: {e}"})


_SPARK_STATUS_TOOL_SCHEMA: dict[str, Any] = {
    "name": "spark_status",
    "description": (
        "Read-only health of the DGX Spark nodes (the two GB10 boxes, spark1 and "
        "spark2). Returns each node's identity, GPU + unified memory, the user-level "
        "toolchain, tool versions, and the high-speed cluster-link state. Free and "
        "fast — no model spend, no side effects. Leave `node` empty to check BOTH. "
        "Use for 'how are the sparks?', 'are the sparks up?', 'check spark2'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "spark1, spark2, or empty/'all' for both nodes."},
        },
    },
}

_SPARK_VERIFY_TOOL_SCHEMA: dict[str, Any] = {
    "name": "spark_verify",
    "description": (
        "Run the full Section-A acceptance on ONE spark node: every good-state gate is "
        "proven TWICE — a machine assertion over the live SSH output AND an independent "
        "Gemini reading of a screenshot of a real macOS Terminal — and a disagreement "
        "is a loud FAIL. Opens Terminal windows + takes screenshots on the Mac and "
        "takes a few minutes; posts a per-gate report to the text channel and saves "
        "PNGs under data/spark/<node>/. Use when the user wants the sparks PROVEN good, "
        "not just pinged."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "spark1 or spark2 (required)."},
            "role": {"type": "string", "description": "Worker role A or B; defaults to the node's role (spark1=A, spark2=B)."},
            "only": {"type": "string", "description": "Optional comma-separated gate ids to run (e.g. 'gpu,mcp'); default all."},
        },
        "required": ["node"],
    },
}

_SPARK_SETUP_TOOL_SCHEMA: dict[str, Any] = {
    "name": "spark_setup",
    "description": (
        "EXECUTABLE: provision/repair a spark node by running ops/spark/setup_node.sh "
        "over SSH (idempotent — installs Claude Code + the user-level toolchain, writes "
        "settings + node identity, registers the filesystem MCP, seeds the API key). "
        "Use to fix a node that spark_verify flagged red, or to bring a fresh node to "
        "the good state. Consequential — prefer wrapping in propose_action for a "
        "tap-to-approve."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "spark1 or spark2 (required)."},
            "role": {"type": "string", "description": "Worker role A or B; defaults to the node's role."},
        },
        "required": ["node"],
    },
}


# ---------------------------------------------------------------------------
# DGX Spark — Claude Code workspace + headless audit/collapse runs.
#
# Aria stands up the SAME Claude Code environment on a spark that the project
# uses on the Mac (the live_visuals_4_CC control-plane), proves its Max-
# subscription auth, launches the forensic audit+collapse as a DETACHED tmux
# run, polls it (decoupled from any live stream — survives disconnects/restarts),
# and pulls results back. One implementation in src/spark.py; scripts/spark_cc.py
# and these tools both call it. A background watcher proactively reports when a
# run ends — the "manage it for me, better than Discord" posture: durable on the
# node, surfaced when done, never dependent on a live connection staying up.
# ---------------------------------------------------------------------------

async def _spark_cc_sync(node: str = "spark1", mirror: bool = False,
                         skip_bootstrap: bool = False, smoke_gate: bool = False,
                         session_key: str = "") -> str:
    """Stand up / update the live_visuals_4 Claude Code workspace on a node."""
    target = (node or "spark1").strip().lower()
    if target not in spark.NODES:
        return json.dumps({"error": f"node is required; known: {_spark_known_nodes()}"})
    await _emit_progress(
        session_key,
        f"syncing the live_visuals_4 Claude Code workspace to {target} "
        "(rsync + overlay + bootstrap; can take several minutes)",
    )
    try:
        res = await asyncio.to_thread(
            spark.sync_workspace, target, mirror=bool(mirror),
            skip_bootstrap=bool(skip_bootstrap), smoke_gate=bool(smoke_gate),
        )
    except Exception as e:
        log.exception("spark_cc_sync failed")
        return json.dumps({"error": f"spark_cc_sync failed: {e}"})
    return json.dumps(res)


async def _spark_cc_auth(node: str = "spark1", probe: bool = False, session_key: str = "") -> str:
    """Report whether a node's claude is authenticated on the Max subscription."""
    target = (node or "spark1").strip().lower()
    if target not in spark.NODES:
        return json.dumps({"error": f"node is required; known: {_spark_known_nodes()}"})
    try:
        return json.dumps(await asyncio.to_thread(spark.cc_auth_status, target, probe=bool(probe)))
    except Exception as e:
        log.exception("spark_cc_auth failed")
        return json.dumps({"error": f"spark_cc_auth failed: {e}"})


async def _spark_run(node: str = "spark1", branch: str = "", mode: str = "",
                     model: str = "", effort: str = "", extended_thinking: bool = False,
                     instruction: str = "", session_key: str = "") -> str:
    """EXECUTABLE: launch the detached forensic audit+collapse run on a node.

    Defaults to the packaged audit+collapse instruction, the standard branch, and
    the audit reasoning policy (Opus 4.8, medium effort, no extended thinking).
    Returns immediately with a run_id; a background watcher reports completion.
    Consequential — Aria offers it via propose_action (tap to approve)."""
    target = (node or "spark1").strip().lower()
    if target not in spark.NODES:
        return json.dumps({"error": f"node is required; known: {_spark_known_nodes()}"})
    instr = (instruction or "").strip()
    if not instr:
        try:
            instr = spark.DEFAULT_AUDIT_INSTRUCTION.read_text()
        except Exception as e:
            return json.dumps({"error": f"no instruction given and default unreadable: {e}"})
    await _emit_progress(session_key, f"launching the audit+collapse run on {target} (detached tmux)")
    try:
        res = await asyncio.to_thread(
            spark.run_audit, target, instr, branch=(branch or None), mode=(mode or spark.DEFAULT_RUN_MODE),
            model=(model or spark.AUDIT_MODEL), effort=(effort or spark.AUDIT_EFFORT),
            extended_thinking=bool(extended_thinking),
        )
    except Exception as e:
        log.exception("spark_run failed")
        return json.dumps({"error": f"spark_run failed: {e}"})
    if res.get("ok"):
        if _post_callback:
            try:
                await _post_callback(
                    f"**Spark run launched — {res['node']}** (`{res['run_id']}`, branch "
                    f"`{res['branch']}`, mode `{res['mode']}`). Detached in tmux; I'll watch it "
                    "and report when it finishes. Poll anytime with spark_run_status."
                )
            except Exception:
                log.debug("spark_run launch post failed", exc_info=True)
        # Decoupled, proactive supervision: outlives this call; if the bot
        # restarts the run keeps going on the node and the user re-attaches.
        asyncio.create_task(
            _watch_spark_run(res["node"], res["run_id"], res.get("branch", "")),
            name=f"spark_watch:{res['run_id']}",
        )
    return json.dumps(res)


async def _spark_run_status(node: str = "spark1", run_id: str = "", session_key: str = "") -> str:
    """Read-only poll of a detached spark run (liveness, exit, branch/commits, last turn, cost)."""
    target = (node or "spark1").strip().lower()
    if target not in spark.NODES:
        return json.dumps({"error": f"node is required; known: {_spark_known_nodes()}"})
    if not (run_id or "").strip():
        return json.dumps({"error": "run_id is required (returned by spark_run)."})
    try:
        return json.dumps(await asyncio.to_thread(spark.run_status, target, run_id.strip()))
    except Exception as e:
        log.exception("spark_run_status failed")
        return json.dumps({"error": f"spark_run_status failed: {e}"})


async def _spark_run_fetch(node: str = "spark1", run_id: str = "", branch: str = "",
                           session_key: str = "") -> str:
    """Pull a finished run's artifacts back to the Mac (run log, refreshed ledger, branch bundle)."""
    target = (node or "spark1").strip().lower()
    if target not in spark.NODES:
        return json.dumps({"error": f"node is required; known: {_spark_known_nodes()}"})
    if not (run_id or "").strip():
        return json.dumps({"error": "run_id is required (returned by spark_run)."})
    await _emit_progress(session_key, f"pulling {target} run {run_id} artifacts back (ledger + branch bundle)")
    try:
        res = await asyncio.to_thread(spark.fetch_results, target, run_id.strip(), branch=(branch or None))
    except Exception as e:
        log.exception("spark_run_fetch failed")
        return json.dumps({"error": f"spark_run_fetch failed: {e}"})
    if _post_callback and res.get("fetched"):
        try:
            extra = f"\nImport the branch: `{res['import_hint']}`" if res.get("import_hint") else ""
            await _post_callback(
                f"**Spark run {run_id} fetched** to `{res.get('local_dir')}` — "
                f"{', '.join(res['fetched'])}.{extra}"
            )
        except Exception:
            log.debug("spark_run_fetch post failed", exc_info=True)
    return json.dumps(res)


async def _watch_spark_run(node: str, run_id: str, branch: str, *,
                           interval: float = 60.0, max_seconds: float = 6 * 3600) -> None:
    """Decoupled supervisor: poll a detached spark run and post once it ends.

    This is independent of any live stream — if the bot restarts mid-run, the
    tmux job keeps going on the node and the user re-attaches with
    spark_run_status. Strictly non-fatal: a poll error never crashes anything."""
    waited = 0.0
    last_heartbeat_turns = -1
    try:
        while waited < max_seconds:
            await asyncio.sleep(interval)
            waited += interval
            try:
                st = await asyncio.to_thread(spark.run_status, node, run_id)
            except Exception:
                log.debug("spark watch poll failed (will retry)", exc_info=True)
                continue
            if st.get("done"):
                rc = st.get("exit_code")
                res = st.get("result") or {}
                clean = (rc == 0) and not res.get("is_error")
                cost = res.get("cost_usd")
                summary = (
                    f"**Spark run {run_id} on {node} "
                    f"{'finished GREEN-path' if clean else 'ended — needs a look'}** — "
                    f"branch `{branch}` (+{st.get('commits_on_branch')} commits, head "
                    f"`{st.get('head_commit')}`), exit {rc}"
                    + (f", ~${cost} notional" if cost is not None else "")
                    + f".\nLast: {(st.get('last_assistant') or '')[:400]}"
                    + f"\nFetch with spark_run_fetch(node={node}, run_id={run_id})."
                )
                if _post_callback:
                    try:
                        await _post_callback(summary)
                    except Exception:
                        log.debug("spark watch completion post failed", exc_info=True)
                if not clean and _alert_callback:
                    try:
                        await _alert_callback(
                            f"Spark run {run_id} on {node} ended non-clean (exit {rc}). "
                            "Inspect with spark_run_status / spark_run_fetch."
                        )
                    except Exception:
                        log.debug("spark watch alert failed", exc_info=True)
                return
            # Sparse heartbeat: only when a fresh batch of turns has landed.
            turns = int(st.get("assistant_turns") or 0)
            if _post_callback and turns and turns >= last_heartbeat_turns + 25:
                last_heartbeat_turns = turns
                try:
                    await _post_callback(
                        f"_spark {node} {run_id}: {turns} turns, on `{st.get('branch')}` "
                        f"(+{st.get('commits_on_branch')} commits)…_"
                    )
                except Exception:
                    log.debug("spark watch heartbeat post failed", exc_info=True)
        log.warning("spark watch for %s exceeded %ss; stopping watcher (run may still be live)",
                    run_id, max_seconds)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("spark watch task crashed")


_SPARK_CC_SYNC_TOOL_SCHEMA: dict[str, Any] = {
    "name": "spark_cc_sync",
    "description": (
        "Stand up or UPDATE the live_visuals_4 Claude Code workspace on a spark node "
        "(rsync the repo + overlay the same .claude/.mcp.json control-plane the Mac uses "
        "+ rebuild venvs/node_modules). Idempotent — this is the 'just update it' path. "
        "Takes a few minutes. Does NOT authenticate claude (that is a one-time `claude /login` "
        "on the node) and never sets ANTHROPIC_API_KEY. Run before the first spark_run."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "spark1 or spark2 (default spark1)."},
            "mirror": {"type": "boolean", "description": "rsync --delete for a pristine re-mirror (drops node-only branch state). Default false."},
            "skip_bootstrap": {"type": "boolean", "description": "Sync+overlay only; skip the venv/node_modules rebuild."},
            "smoke_gate": {"type": "boolean", "description": "Run scripts/quality_gate.sh after bootstrap to record a baseline."},
        },
    },
}

_SPARK_CC_AUTH_TOOL_SCHEMA: dict[str, Any] = {
    "name": "spark_cc_auth",
    "description": (
        "Check whether a spark node's claude is logged into the Max subscription (the "
        "cheap check is the OAuth creds file; probe=true spends one tiny call to confirm a "
        "live round-trip). If not authed, the user must run `ssh -t <node>` then `claude` "
        "-> `/login` once. Use before launching a run."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "spark1 or spark2 (default spark1)."},
            "probe": {"type": "boolean", "description": "Spend one tiny subscription call to confirm a real round-trip. Default false."},
        },
    },
}

_SPARK_RUN_TOOL_SCHEMA: dict[str, Any] = {
    "name": "spark_run",
    "description": (
        "EXECUTABLE: launch the forensic AUDIT + COLLAPSE run on a spark node as a detached "
        "tmux job (survives disconnects). It refreshes the collapse ledger against HEAD, then "
        "performs the collapses wave-by-wave on a new branch, running the quality gate after "
        "each wave and halting loudly on RED. Defaults to the audit reasoning policy: Opus 4.8, "
        "medium effort, no extended thinking. Returns a run_id immediately; Aria watches it and "
        "reports when it finishes. Requires spark_cc_sync done + the node on the Max "
        "subscription. Consequential — offer via propose_action (tap to approve)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "spark1 or spark2 (default spark1)."},
            "branch": {"type": "string", "description": "Branch to create/use; default collapse/<date>."},
            "mode": {"type": "string", "description": "Claude permission mode: bypassPermissions (default, autonomous), acceptEdits, plan, or default."},
            "model": {"type": "string", "description": "Model slug; default claude-opus-4-8."},
            "effort": {"type": "string", "description": "Adaptive reasoning effort: low/medium/high/xhigh/max. Default medium (the audit policy)."},
            "extended_thinking": {"type": "boolean", "description": "Enable extended thinking. Default false (the audit policy)."},
            "instruction": {"type": "string", "description": "Override the run instruction; default is the packaged audit+collapse brief."},
        },
    },
}

_SPARK_RUN_STATUS_TOOL_SCHEMA: dict[str, Any] = {
    "name": "spark_run_status",
    "description": (
        "Read-only poll of a detached spark run (from spark_run): is it still running, did it "
        "finish + exit code, current branch and commit count, the last assistant turn / current "
        "tool, and notional cost. Decoupled from any live stream — safe to call anytime."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "spark1 or spark2 (default spark1)."},
            "run_id": {"type": "string", "description": "The run id returned by spark_run (required)."},
        },
        "required": ["run_id"],
    },
}

_SPARK_RUN_FETCH_TOOL_SCHEMA: dict[str, Any] = {
    "name": "spark_run_fetch",
    "description": (
        "Pull a finished spark run's artifacts back to the Mac: the run log, the refreshed "
        "collapse ledger, and an importable git bundle of the collapse branch (with the exact "
        "git command to import it into the local live_visuals_4 and push)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "spark1 or spark2 (default spark1)."},
            "run_id": {"type": "string", "description": "The run id returned by spark_run (required)."},
            "branch": {"type": "string", "description": "Branch to bundle; default = the run's current branch."},
        },
        "required": ["run_id"],
    },
}


# The agent's handle on the ground table: when it discovers where something
# important lives (or the user names it), it binds the referent durably so the
# NEXT request resolves it from context instead of re-discovering at Opus
# prices. The read side needs no tool — _build_context renders ground into
# every loop's first message.
_SET_GROUND_TOOL_SCHEMA: dict[str, Any] = {
    "name": "set_ground",
    "description": (
        "Bind a role in the durable working set (ground) to a concrete "
        "artifact so future requests resolve referents like 'the plan' or "
        "'that project' instantly from context. Call it when you locate "
        "something the user will refer back to (a plan document, a project "
        "directory, a deliverable) or when the user declares what they're "
        "working on. Common roles: active_plan, active_project, "
        "last_artifact; short custom roles are fine."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "role": {
                "type": "string",
                "description": "Binding name, e.g. 'active_plan'.",
            },
            "label": {
                "type": "string",
                "description": "Short human description of the artifact.",
            },
            "path": {
                "type": "string",
                "description": "Absolute path when the artifact is a file or directory.",
            },
            "detail": {
                "type": "string",
                "description": "How to read it (thread id, command, location notes).",
            },
        },
        "required": ["role", "label"],
    },
}


async def _set_ground_tool(
    role: str,
    label: str,
    path: str = "",
    detail: str = "",
    session_key: str = "",
) -> str:
    set_ground(role, label, path or None, detail or None,
               source=session_key or "agent")
    return json.dumps({"ok": True, "role": role, "label": label,
                       "path": path or None})


_ASK_USER_TOOL_SCHEMA: dict[str, Any] = {
    "name": "ask_user",
    "description": (
        "Ask the user an OPEN question and BLOCK for their typed/spoken reply, "
        "returning the answer. Use when you need an open answer mid-task that a "
        "yes/no can't carry — including relaying a Claude Code thread's pending "
        "question back to the user and feeding their answer to claude_code_send. "
        "For a simple approve/skip, prefer propose_action."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question to put to the user."},
        },
        "required": ["question"],
    },
}


async def _ask_user_tool(question: str = "", session_key: str = "") -> str:
    """Ask the user an open question and block for the reply."""
    if _ask_callback is None:
        return json.dumps({"error": "ask_user is not wired (no Discord surface)."})
    if not (question or "").strip():
        return json.dumps({"error": "question is required."})
    answer = await _ask_callback(question, session_key)
    if not answer:
        return json.dumps({"answered": False, "note": "No reply before timeout."})
    return json.dumps({"answered": True, "answer": answer})


# modelvault cold-backup bridge. Aria does NOT do the transfer — she launches the
# diskless modelvault cloud runner (a sibling repo), which spins up an ephemeral
# GCE VM that streams the model into encrypted GCS and self-deletes. The script
# returns in seconds (it only starts the job), so this tool is "send a link and
# walk away". The engine lives in its own repo; this is just the trigger.
_MODELVAULT_CLOUD_BACKUP_SH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),  # <agi_env_v1>
    "modelvault", "ops", "cloud_backup.sh",
)

_BACKUP_MODEL_TOOL_SCHEMA: dict[str, Any] = {
    "name": "backup_model",
    "description": (
        "Back up a Hugging Face model to encrypted cold storage. Launches a diskless "
        "modelvault backup on an ephemeral cloud VM that streams the model into GCS and "
        "self-deletes; returns right away (it starts the job, it does NOT wait for the "
        "multi-terabyte transfer). Use when the user says 'back up <model>', e.g. 'back "
        "up huggingface.co/org/model'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Model URL or id, e.g. 'huggingface.co/org/model' or 'org/model'."},
        },
        "required": ["url"],
    },
}


async def _backup_model(url: str = "", session_key: str = "") -> str:
    """Launch a diskless modelvault cloud backup of a model URL. Returns fast.

    Path is config-determined (not a fallback): if MODELVAULT_LAUNCHER_SSH is set we
    trigger on the always-on launcher VM over plain SSH — no local gcloud, so it
    survives the org reauth policy ("DM a link and walk away"). Otherwise we run the
    cloud runner locally (needs gcloud authed on this machine).
    """
    import shlex

    target = (url or "").strip()
    if not target:
        return json.dumps({"error": "url is required (e.g. huggingface.co/org/model)"})
    await _emit_progress(session_key, f"launching cloud backup of {target}")

    launcher = os.getenv("MODELVAULT_LAUNCHER_SSH", "").strip()
    if launcher:
        key = os.path.expanduser(os.getenv("MODELVAULT_LAUNCHER_KEY", "~/.ssh/modelvault_launcher"))
        argv = ["ssh", "-i", key, "-o", "StrictHostKeyChecking=accept-new",
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", launcher,
                f"bash ~/modelvault/ops/cloud_backup.sh {shlex.quote(target)}"]
    else:
        if not os.path.exists(_MODELVAULT_CLOUD_BACKUP_SH):
            return json.dumps({"error": f"modelvault cloud_backup.sh not found at {_MODELVAULT_CLOUD_BACKUP_SH}"})
        argv = ["bash", _MODELVAULT_CLOUD_BACKUP_SH, target]

    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return json.dumps({"ok": False, "url": target, "error": "backup launch did not return within 180s"})
    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()
    if proc.returncode != 0:
        # Loud, with the fix (e.g. gcloud auth). Never a silent failure.
        return json.dumps({"ok": False, "url": target,
                           "error": f"backup launch failed (rc={proc.returncode})",
                           "detail": (err or out)[-800:]})
    return json.dumps({"ok": True, "url": target, "launched": True,
                       "via": "launcher" if launcher else "local", "status": out[-800:]})


# Local (non-MCP) tools the do_with_claude loop can dispatch alongside the MCP
# catalog. create_42c_account provisions a login deterministically; the cursor_*
# tools give the text agent the same durable Cursor-thread introspection and
# dispatch Aria has in voice, so "#ucs: what are the live_visuals_4 threads?" is
# answered from transcripts, not two clobbered watch events; the spark_* tools
# give it the same handle on the GB10 nodes Aria has in voice. One table, one
# dispatch site — no per-tool special-casing.
_LOCAL_TOOL_SCHEMAS: list[dict[str, Any]] = [
    _CREATE_42C_TOOL_SCHEMA,
    _CURSOR_THREADS_TOOL_SCHEMA,
    _CURSOR_READ_TOOL_SCHEMA,
    _CURSOR_SEND_TOOL_SCHEMA,
    _CURSOR_SPAWN_TOOL_SCHEMA,
    _CURSOR_AGENTS_TOOL_SCHEMA,
    _CLAUDE_CODE_SPAWN_TOOL_SCHEMA,
    _CLAUDE_CODE_SEND_TOOL_SCHEMA,
    _CLAUDE_CODE_READ_TOOL_SCHEMA,
    _CLAUDE_CODE_THREADS_TOOL_SCHEMA,
    _ASK_USER_TOOL_SCHEMA,
    _SPARK_STATUS_TOOL_SCHEMA,
    _SPARK_VERIFY_TOOL_SCHEMA,
    _SPARK_SETUP_TOOL_SCHEMA,
    _SPARK_CC_SYNC_TOOL_SCHEMA,
    _SPARK_CC_AUTH_TOOL_SCHEMA,
    _SPARK_RUN_TOOL_SCHEMA,
    _SPARK_RUN_STATUS_TOOL_SCHEMA,
    _SPARK_RUN_FETCH_TOOL_SCHEMA,
    _SET_GROUND_TOOL_SCHEMA,
    _BACKUP_MODEL_TOOL_SCHEMA,
]
_LOCAL_TOOL_HANDLERS: dict[str, Callable[..., Coroutine[Any, Any, str]]] = {
    "create_42c_account": _create_42c_account,
    "cursor_threads": _cursor_threads,
    "cursor_read": _cursor_read,
    "cursor_send": _cursor_send,
    "cursor_spawn": _cursor_spawn,
    "cursor_agents": _cursor_agents,
    "claude_code_spawn": _claude_code_spawn,
    "claude_code_send": _claude_code_send,
    "claude_code_read": _claude_code_read,
    "claude_code_threads": _claude_code_threads,
    "ask_user": _ask_user_tool,
    "spark_status": _spark_status,
    "spark_verify": _spark_verify,
    "spark_setup": _spark_setup,
    "spark_cc_sync": _spark_cc_sync,
    "spark_cc_auth": _spark_cc_auth,
    "spark_run": _spark_run,
    "spark_run_status": _spark_run_status,
    "spark_run_fetch": _spark_run_fetch,
    "set_ground": _set_ground_tool,
    "backup_model": _backup_model,
}

# Local tools that receive the loop's session_key automatically.
_SESSION_KEY_LOCAL_TOOLS = frozenset({
    "create_42c_account",
    "spark_status", "spark_verify", "spark_setup",
    "spark_cc_sync", "spark_cc_auth", "spark_run", "spark_run_status", "spark_run_fetch",
    "set_ground",
    "ask_user",
    "backup_model",
})


# ---------------------------------------------------------------------------
# Context economics — prompt-cache breakpoints and tool-result compaction.
#
# The honeycomb forensic (2026-06-12): a 7-iteration loop re-billed its
# ~20K-token static prefix and every carried tool dump at full Opus input
# price each step ($0.49 → $1.17 per iteration), then a second run re-bought
# the same discovery. These helpers make iteration N re-READ what iteration
# N-1 paid for (cache breakpoints) and keep the carried history bounded
# (compaction), so the $5 loop cap buys work instead of repeat billing.
# ---------------------------------------------------------------------------

def _cache_marked_system(system_prompt: str) -> list[dict[str, Any]]:
    """System prompt as a cache-marked block (caches system + tool catalog)."""
    return [{
        "type": "text",
        "text": system_prompt,
        "cache_control": {"type": "ephemeral"},
    }]


def _cache_marked_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark the last tool so the whole catalog prefix is cacheable."""
    if not tools:
        return tools
    marked = list(tools)
    marked[-1] = dict(marked[-1])
    marked[-1]["cache_control"] = {"type": "ephemeral"}
    return marked


def _move_message_cache_breakpoint(messages: list[dict[str, Any]]) -> None:
    """Keep exactly one moving cache breakpoint on the newest user message.

    Strips stale markers from every dict content block (Anthropic allows max
    4 breakpoints per request; system + tools hold two), then marks the last
    block of the most recent user message so the entire conversation prefix
    is a cache hit on the next iteration. Assistant messages carry SDK
    objects, not dicts — they are left untouched.
    """
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict):
                block.pop("cache_control", None)
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list) and content and isinstance(content[-1], dict):
            content[-1]["cache_control"] = {"type": "ephemeral"}
        return


def _compact_old_tool_results(messages: list[dict[str, Any]]) -> int:
    """Clip tool results older than the last `_COMPACT_KEEP_FULL` carriers.

    The model saw the full text when it was fresh and acted on it; what later
    iterations need is the gist, not a re-billed 30KB dump. Returns how many
    blocks were compacted. Idempotent: already-clipped blocks are shorter
    than the threshold and are never re-cut.
    """
    carriers = [
        m for m in messages
        if m.get("role") == "user"
        and isinstance(m.get("content"), list)
        and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in m["content"]
        )
    ]
    compacted = 0
    for m in carriers[:-_COMPACT_KEEP_FULL] if _COMPACT_KEEP_FULL else carriers:
        for b in m["content"]:
            if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                continue
            c = b.get("content")
            if isinstance(c, str) and len(c) > _COMPACT_HEAD_CHARS + 300:
                b["content"] = (
                    c[:_COMPACT_HEAD_CHARS]
                    + f"\n[…compacted: {len(c):,} chars total. You already saw "
                    "the full result when it was fresh; re-run the tool only "
                    "if you genuinely need the rest.]"
                )
                compacted += 1
    return compacted


def _distill_findings(tool_trace: list[dict], cap_chars: int = 6_000) -> str:
    """Mechanical findings ledger from a loop's tool trace.

    Deterministic and free (no model call): one line per executed tool —
    what was asked, the head of what came back. Enough for the next run in
    this thread to build on located paths and read files instead of
    re-buying the discovery (the honeycomb $6-then-$5.20 pattern).
    """
    if not tool_trace:
        return ""
    lines: list[str] = []
    for t in tool_trace[-24:]:
        if t.get("deduped") or "_dropped_tool_calls" in t:
            continue
        try:
            args = json.dumps(t.get("args", {}), default=str)
        except (TypeError, ValueError):
            args = repr(t.get("args"))
        res = " ".join(str(t.get("result", "")).split())
        lines.append(f"- {t.get('tool')} {args[:200]} => {res[:300]}")
    text = "\n".join(lines)
    return text[-cap_chars:] if len(text) > cap_chars else text


def _blocker_with_findings(reason: str, need: str, tool_trace: list[dict]) -> str:
    """A spend-stop blocker that hands over what the spend bought.

    The honeycomb run paused at its cost wall having already read the
    answer's files — and returned none of it. A stop on spend must never
    withhold paid-for findings: 'keep going' resumes from the ledger, and
    the user can often answer themselves from the digest alone.
    """
    msg = format_block(reason, need)
    digest = _distill_findings(tool_trace, cap_chars=1_500)
    if digest:
        msg += f"\n\nWhat I established before stopping:\n{digest}"
    return msg


async def _do_with_claude(
    task: str,
    session_key: str = "",
) -> str:
    # One request == one Discord thread == one session_key, so two top-level
    # requests never share this lock — they run in parallel, each isolated.
    # A follow-up typed into the SAME thread while its loop is still running
    # serializes behind it (it awaits the lock) instead of being rejected.
    #
    # The old `if lock.locked(): return {"error": "...already running..."}`
    # envelope was a symptom-handler for the previous channel-keyed design,
    # where unrelated `#ucs` requests collided on one lock (judged failure
    # session 144). Thread-per-request removes the collision at the root, so
    # the rejection is deleted, not reworded. Out-of-band control (`!stop`)
    # sets the per-session cancel flag directly and does not pass through
    # this lock, so a stuck loop is still interruptible while queued.
    lock = _agent_lock_for(session_key or "global")
    async with lock:
        return await _do_with_claude_loop(task, session_key)


async def _start_task(goal: str = "", session_key: str = "") -> str:
    """Start a durable, backgroundable Task (Primitive 1). The agent loop advances
    it out-of-band, so the user can walk away. Returns immediately with the id;
    the Task outlives this voice session and is checked via task_status."""
    from . import tasks

    goal = (goal or "").strip()
    if not goal:
        return json.dumps({"error": "start_task needs a goal"})

    async def _engine(g: str, sk: str) -> str:
        return await _do_with_claude(g, session_key=sk)

    task_id = tasks.start_task(goal, _engine, session_key=session_key)
    return (
        f"Started task #{task_id}: {goal[:120]}. I'll work on it in the background "
        f"and ping you when it's done or if I hit a wall. "
        f'Ask "how\'s task {task_id}?" anytime.'
    )


async def _task_status(task_id: str = "", session_key: str = "") -> str:
    """Read a durable Task — how "how's X going?" is answered, from the Task
    object, not the chat. With no id, reports active tasks (or the most recent)."""
    from . import tasks
    from .db import get_task, latest_task, list_tasks

    if task_id:
        try:
            t = get_task(int(str(task_id).lstrip("#").strip()))
        except (TypeError, ValueError):
            t = None
        if not t:
            return json.dumps({"error": f"no task {task_id}"})
        summary = tasks.task_summary(t)
        body = (t.get("transcript") or "").strip()
        return f"{summary}\n\n{body}" if body else summary

    active = list_tasks(statuses=("queued", "running", "needs_you"), limit=10)
    if active:
        return "Active tasks:\n" + "\n".join(tasks.task_summary(t) for t in active)
    t = latest_task()
    return tasks.task_summary(t) if t else "No tasks yet."


async def _run_playbook(name: str = "", session_key: str = "") -> str:
    """Run a named playbook — an ordered list of Tasks — in the background. The
    payoff: name it, walk away, get pinged as each step finishes or on a halt."""
    from . import playbook

    name = (name or "").strip()
    if not name:
        avail = ", ".join(playbook.list_playbooks()) or "(none)"
        return json.dumps({"error": f"run_playbook needs a name. Available: {avail}"})

    async def _engine(g: str, sk: str) -> str:
        return await _do_with_claude(g, session_key=sk)

    try:
        n = playbook.start_playbook(name, _engine)
    except (FileNotFoundError, ValueError):
        avail = ", ".join(playbook.list_playbooks()) or "(none yet)"
        return json.dumps({"error": f"no runnable playbook '{name}'. Available: {avail}"})
    return (
        f"Running playbook '{name}' ({n} steps) in the background. I'll work "
        f"through them in order and ping you as each finishes — or stop and ask if "
        f'one hits a wall. Walk away; ask "how are my tasks?" anytime.'
    )


async def _list_playbooks(session_key: str = "") -> str:
    """List the available playbooks (ordered Task sequences in workflows/)."""
    from . import playbook

    names = playbook.list_playbooks()
    if not names:
        return "No playbooks yet — add one at workflows/<name>.playbook.md (a numbered list of steps)."
    return "Playbooks: " + ", ".join(names)


async def _do_with_claude_loop(
    task: str,
    session_key: str = "",
) -> str:
    state = _state_for(session_key)
    state.cancel = False
    if not _anthropic_client:
        return json.dumps({"error": "Anthropic client not initialized"})

    try:
        from .mcp import mcp_client
        if not mcp_client:
            return json.dumps({"error": "MCP client not initialized"})
        # Local deterministic tools the loop can dispatch alongside MCP tools
        # (account provisioning + the durable Cursor-thread surface). Dispatched
        # below via _LOCAL_TOOL_HANDLERS — one table, no per-tool branch.
        tools = mcp_client.list_tools_anthropic() + _LOCAL_TOOL_SCHEMAS
    except ImportError:
        return json.dumps({"error": "MCP module not available"})

    # Cache-marked static prefix: the tool catalog and system prompt are
    # identical every iteration (and across loops within the cache TTL), so
    # they are paid for once and read at 0.10x after that.
    tools = _cache_marked_tools(tools)
    system_prompt = load_template("do_with_claude_system")
    system_blocks = _cache_marked_system(system_prompt)

    memories = mem_recall(task, limit=3)
    memory_ctx = ""
    if memories:
        memory_ctx = "Relevant memories:\n" + "\n".join(
            f"- {m.get('memory', m.get('text', ''))}" for m in memories
        ) + "\n\n"

    # Findings ledger — what a previous run in THIS thread already
    # established. A budget-paused run's discovery survives the wall; "keep
    # going" resumes from here instead of re-buying it (honeycomb forensic).
    findings_ctx = ""
    if session_key:
        prior = get_findings(session_key)
        if prior:
            findings_ctx = (
                "Findings already established by this thread's previous run "
                f"(status: {prior['status']}, {_rel_age(prior['updated_at'])}) "
                "— build on these; do NOT re-run discovery for anything "
                "listed here:\n"
                f"{prior['findings']}\n\n"
            )

    context_block = _build_context(session_key)
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": [{
            "type": "text",
            "text": context_block + findings_ctx + memory_ctx + task,
        }],
    }]

    max_iterations = config.do_with_claude_max_iterations
    iteration = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost = 0.0
    last_iter_cost = 0.0
    max_tokens_budget = 50000
    started_at = time.monotonic()
    started_at_iso = _now_iso()
    final_status = "completed"
    tool_trace: list[dict] = []

    # Cross-iteration (and in-batch) dedup ledger.
    # Maps (tool_name, args_hash) -> (call_count, cached_result_str). When
    # Claude re-emits the same tool call inside this agent loop, we short-
    # circuit with the cached result and a "_dup_hit" marker so the model
    # sees a clear signal it has already asked this. This is the L5 fix from
    # the audit (the 6x search_emails-in-15s case).
    called_tools: dict[str, tuple[int, str]] = {}

    # P1 retry counter — each anchor-driven retry extends the effective
    # iteration cap by one so a single gate failure can't starve the budget
    # the agent legitimately needed for tool work.
    ground_check_retries = 0

    # Outcome accounting — one classifier (src/outcomes.py) reads the *meaning*
    # of every tool result and says continue / retry-once / stop. `retry_used`
    # budgets one retry per transient family; `blocker` carries the single
    # user-facing message when we hit a wall (a permanent failure, an exhausted
    # transient, a decline, or the per-loop cost cap).
    retry_used: dict[str, int] = {}
    blocker: str | None = None

    # True while every executed tool call is a discovery action — the signal
    # that the task's referent is still unresolved and spend is going to
    # archaeology, not work.
    discovery_only = True

    def _save_ledger(status: str) -> None:
        """Persist this run's findings for the thread's next run. Telemetry-
        class write: never breaks the hot path, but never silent either."""
        if not session_key:
            return
        try:
            text = _distill_findings(tool_trace)
            if text:
                save_findings(session_key, text, status)
        except Exception:
            log.warning("findings ledger write failed session=%s", session_key,
                        exc_info=True)

    while iteration < max_iterations + ground_check_retries and not state.cancel:
        iteration += 1

        # Pre-spend cap: stop BEFORE the call that would cross the line.
        # The honeycomb run charged $6.00 against a $5.00 cap because the
        # check ran after the spend; projecting from the last iteration's
        # cost closes that hole.
        if iteration > 1 and total_cost + last_iter_cost >= _LOOP_COST_CAP_USD:
            final_status = "blocked"
            blocker = _blocker_with_findings(
                f"this task is about to exceed its ${_LOOP_COST_CAP_USD:.2f} "
                f"budget (spent ${total_cost:.2f} over {iteration - 1} steps)",
                "a go-ahead to keep spending on it, or a narrower task",
                tool_trace,
            )
            if _alert_callback:
                asyncio.create_task(_alert_callback(blocker))
            break

        # One moving cache breakpoint on the newest user message: the whole
        # conversation prefix becomes a cache read on the next iteration.
        _move_message_cache_breakpoint(messages)

        response = await asyncio.to_thread(
            _anthropic_client.messages.create,
            model=config.claude_model,
            system=system_blocks,
            messages=messages,
            tools=tools,
            max_tokens=config.do_with_claude_max_output_tokens,
        )

        usage = response.usage
        if usage:
            total_input_tokens += _usage_context_tokens(usage)
            total_output_tokens += usage.output_tokens
            cost = _usage_cost(usage)
            total_cost += cost
            last_iter_cost = cost
            log_event("do_with_claude_iteration", {"iteration": iteration}, "", 0, session_key, cost)

        if response.stop_reason == "end_turn" or not any(
            b.type == "tool_use" for b in response.content
        ):
            text_parts = [b.text for b in response.content if hasattr(b, "text") and b.text]
            result = "\n".join(text_parts)

            # P1 — Pre-send Grounding Gate. Run anchors against the draft;
            # if any are degraded/failed AND retry budget remains, feed
            # violations back to Aria as a user message and continue.
            if ground_check_retries < _GROUND_CHECK_MAX_RETRIES:
                violations = await _ground_check(tool_trace, result, session_key)
                if violations:
                    ground_check_retries += 1
                    log.warning(
                        "ground-check retry %d/%d session=%s",
                        ground_check_retries,
                        _GROUND_CHECK_MAX_RETRIES,
                        session_key,
                    )
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": _ground_check_user_message(violations),
                        }],
                    })
                    continue

            log_loop_execution(
                tool_name="do_with_claude",
                session_key=session_key,
                prompt_template="do_with_claude_system",
                model_id=config.claude_model,
                tokens_in=total_input_tokens,
                tokens_out=total_output_tokens,
                cost_usd=total_cost,
                latency_ms=int((time.monotonic() - started_at) * 1000),
                iterations=iteration,
                status="completed",
                started_at=started_at_iso,
            )
            # Postcondition gate: never let a world-changing action stand as
            # "done" when its own result carried a hard failure signal. Append
            # an honest, factual "could not confirm" note rather than letting
            # the narration assert success (the "Done, I emailed it" failure).
            unverified = unverified_world_changes(tool_trace)
            if unverified:
                result = result + (
                    "\n\n---\n_Unverified: "
                    + ", ".join(unverified)
                    + " — I could not confirm these actually succeeded / were "
                    "delivered (failure signal or no confirmation in the tool "
                    "result); treat them as not done until confirmed._"
                )
            _save_ledger("completed")
            state.last_tool_trace = _cap_trace_size(tool_trace) or None
            return result

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_args = dict(block.input) if block.input else {}
            dedup_key = _dedup_key(block.name, tool_args)
            prev_count, cached_result = called_tools.get(dedup_key, (0, ""))

            if prev_count >= 1:
                # Loud signal back to the model: the call is unchanged, and
                # we are NOT going to spend another upstream round-trip on it.
                # If Claude is iterating because the result wasn't sufficient,
                # this forces a different next move.
                result_str = json.dumps({
                    "_dup_hit": True,
                    "_call_count": prev_count + 1,
                    "_note": (
                        f"You have already called {block.name} with these "
                        f"exact args earlier in this session. The cached "
                        f"result is included below. Stop re-issuing this "
                        f"call; if the result is insufficient, change the "
                        f"args or pick a different tool."
                    ),
                    "cached_result": cached_result[:40_000],
                })
                called_tools[dedup_key] = (prev_count + 1, cached_result)
                log.warning(
                    "Claude dedup hit: %s (call #%d) session=%s",
                    block.name, prev_count + 1, session_key,
                )
            else:
                await _emit_progress(session_key, _humanize_step(block.name, tool_args))
                local_handler = _LOCAL_TOOL_HANDLERS.get(block.name)
                if local_handler is not None:
                    local_args = dict(tool_args)
                    if block.name in _SESSION_KEY_LOCAL_TOOLS:
                        local_args.setdefault("session_key", session_key)
                    result_str = await _invoke_handler(local_handler, local_args)
                else:
                    tool_result = await mcp_client.call_tool(
                        block.name, tool_args, session_key=session_key,
                    )
                    result_str = tool_result
                called_tools[dedup_key] = (1, result_str)
                if not is_discovery_family(_action_family(block.name, tool_args)):
                    discovery_only = False

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str[:50_000],
            })
            tool_trace.append({
                "tool": block.name,
                "args": _truncate_trace_args(tool_args),
                "result": result_str[:1_000_000],
                "result_chars": len(result_str),
                "result_truncated": len(result_str) > 1_000_000,
                "deduped": prev_count >= 1,
            })

            # One deterministic classifier decides what this result means.
            # BLOCKED (a permanent wall or a decline) stops at the first
            # occurrence — no count threshold to defeat, no exit-code to mask.
            # TRANSIENT gets one bounded retry per family, then becomes a wall.
            outcome = classify_outcome(block.name, tool_args, result_str)
            if outcome.is_blocked:
                log.warning(
                    "blocked: %s args=%s reason=%s session=%s",
                    block.name, str(tool_args)[:200],
                    outcome.reason[:200], session_key,
                )
                # A wall carries the partial work + the one ask — never an empty
                # "Blocked". "Here's what I got, here's what I couldn't do, here's
                # the one thing I need" is what a chief of staff hands you.
                blocker = _blocker_with_findings(outcome.reason, outcome.need, tool_trace)
                break
            if outcome.is_transient:
                used = retry_used.get(outcome.family, 0)
                if used >= TRANSIENT_RETRY_BUDGET:
                    log.warning(
                        "transient exhausted: %s family=%s reason=%s session=%s",
                        block.name, outcome.family,
                        outcome.reason[:200], session_key,
                    )
                    blocker = _blocker_with_findings(
                        outcome.reason,
                        outcome.need or "a stable connection, or a different approach",
                        tool_trace,
                    )
                    break
                retry_used[outcome.family] = used + 1
                log.info(
                    "transient retry budgeted: %s family=%s (used=%d) session=%s",
                    block.name, outcome.family, used + 1, session_key,
                )

        messages.append({"role": "user", "content": tool_results})

        # Keep the carried history bounded: results older than the last two
        # carriers are clipped to their head (the model saw them in full when
        # they were fresh). This is what keeps per-iteration cost flat.
        _compact_old_tool_results(messages)

        if blocker is not None:
            final_status = "blocked"
            if _alert_callback:
                asyncio.create_task(_alert_callback(blocker))
            break

        # Discovery backstop: meaningful spend with NOTHING but find/grep/list
        # so far means the task's referent never resolved — more searching is
        # a grind. Stop and ask the one question. With ground + the projects
        # map in context this should almost never fire.
        if discovery_only and total_cost >= _DISCOVERY_COST_CAP_USD:
            final_status = "blocked"
            blocker = _blocker_with_findings(
                f"I've spent ${total_cost:.2f} purely on discovery "
                f"(find/grep/list) without resolving what the task refers to",
                "the concrete path or name — or bind it once with set_ground "
                "/ projects/registry.md and I'll never have to search for it "
                "again",
                tool_trace,
            )
            if _alert_callback:
                asyncio.create_task(_alert_callback(blocker))
            break

        if total_output_tokens > max_tokens_budget:
            final_status = "token_budget"
            if _alert_callback:
                asyncio.create_task(_alert_callback(
                    f"do_with_claude token budget exceeded ({total_output_tokens} tokens)"
                ))
            break

        # Hard stop if a single iteration blew straight past the cap despite
        # the pre-spend projection — same wall, same formatter, and the
        # paid-for findings ride along instead of being withheld.
        if total_cost >= _LOOP_COST_CAP_USD:
            final_status = "blocked"
            blocker = _blocker_with_findings(
                f"this one task hit its ${_LOOP_COST_CAP_USD:.2f} budget "
                f"(spent ${total_cost:.2f} over {iteration} steps) before finishing",
                "a go-ahead to keep spending on it, or a narrower task",
                tool_trace,
            )
            if _alert_callback:
                asyncio.create_task(_alert_callback(blocker))
            break

    if state.cancel:
        final_status = "cancelled"
    elif final_status not in ("token_budget", "blocked"):
        final_status = "iteration_limit"

    log_loop_execution(
        tool_name="do_with_claude",
        session_key=session_key,
        prompt_template="do_with_claude_system",
        model_id=config.claude_model,
        tokens_in=total_input_tokens,
        tokens_out=total_output_tokens,
        cost_usd=total_cost,
        latency_ms=int((time.monotonic() - started_at) * 1000),
        iterations=iteration,
        status=final_status,
        started_at=started_at_iso,
    )

    _save_ledger(final_status)
    state.last_tool_trace = _cap_trace_size(tool_trace) or None

    if state.cancel:
        return "Task cancelled by user."

    # A wall (permanent failure / exhausted transient / decline / cost cap):
    # return the actionable blocker instead of the generic "iteration limit /
    # partial progress" string (which the judge auto-fails and which tells the
    # user nothing about what to do next).
    if blocker is not None:
        return blocker

    partial = (
        f"Task reached iteration limit ({max_iterations}). Partial progress made."
    )
    if _alert_callback:
        asyncio.create_task(_alert_callback(partial))
    return partial


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------

async def suggest_next_action(project: str, summary: str) -> str:
    """One cheap Claude call: given what a coding thread just finished, return a
    single imperative next-action sentence, or '' if nothing obvious.

    Turns a useless "thread is over" ping into a "here's the next move, approve?"
    decision. Bounded to a tiny completion so it's cheap to run per completion.
    """
    if not _anthropic_client or not summary.strip():
        return ""
    try:
        resp = await asyncio.to_thread(
            _anthropic_client.messages.create,
            model=config.claude_model,
            max_tokens=200,
            system=(
                "A coding/agent thread just finished. Propose the SINGLE most "
                "valuable next action the user should approve. Reply with ONE "
                "imperative sentence (the action itself, ready to execute), or "
                "exactly 'NONE' if the work looks complete with no obvious next "
                "step. No preamble, no markdown."
            ),
            messages=[{
                "role": "user",
                "content": f"Project: {project}\nWhat just finished:\n{summary[:2000]}",
            }],
        )
        text = "".join(
            b.text for b in resp.content if hasattr(b, "text") and b.text
        ).strip()
        return "" if text.upper().startswith("NONE") else text[:300]
    except Exception:
        log.exception("suggest_next_action failed")
        return ""


async def _remember(text: str) -> str:
    mem_remember(text)
    return json.dumps({"ok": True, "remembered": text[:100]})


async def _recall(query: str) -> str:
    results = mem_recall(query, limit=5)
    memories = [m.get("memory", m.get("text", "")) for m in results]
    return json.dumps({"memories": memories})


# ---------------------------------------------------------------------------
# Cancel / Confirm
# ---------------------------------------------------------------------------

async def _cancel_current_task(session_key: str = "") -> str:
    """Cancel the agent loop. Broadcasts across every session by default.

    Called via Gemini tool dispatch when the user says stop/abort. The
    voice user typically has one active loop, but text users could have
    another running in parallel — we treat "stop" as the same emergency
    semantics as `!stop`: kill everything. If a future caller wants to
    target a single session, pass session_key explicitly.
    """
    if session_key:
        _state_for(session_key).cancel = True
        target = session_key
    else:
        for s in _session_states.values():
            s.cancel = True
        target = "all"
    if _alert_callback:
        asyncio.create_task(_alert_callback("Task cancelled via voice command."))
    return json.dumps({"ok": True, "cancelled": True, "target": target})


async def _confirm_action_noop(**kwargs) -> str:
    """confirm_action is handled directly in GeminiSession, not here."""
    return json.dumps({"ok": True, "note": "handled by Gemini session"})


async def _propose_action(
    title: str,
    why: str = "",
    task: str = "",
    session_key: str = "",
) -> str:
    """Recommend a consequential approach Corbin can approve with one tap.

    Posts a context-rich card to his phone (DM) and #ucs-alerts; on approval
    the task runs autonomously (handed to do_with_claude) with no further
    per-command confirmation. This is the decision surface that replaces
    confirming individual commands. Returns immediately with an ack — the
    wait-for-approval and execution happen in the background.
    """
    if not _propose_callback:
        return json.dumps({"error": "propose_action is not wired in this process"})
    if not title or not task:
        return json.dumps({"error": "title and task are required"})
    try:
        ack = await _propose_callback(title, why, task, session_key)
        return json.dumps(ack if isinstance(ack, dict) else {"ok": True, "proposed": title})
    except Exception as e:
        log.exception("propose_action failed")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Quick-access read-only MCP shortcuts (bypass do_with_claude)
# ---------------------------------------------------------------------------

def _find_mcp_tool(*keyword_groups: tuple[str, ...]) -> str | None:
    """Return the first MCP tool whose name contains all keywords in any group."""
    try:
        from .mcp import mcp_client
    except ImportError:
        return None
    if not mcp_client:
        return None
    names = list(mcp_client._tools.keys())
    for group in keyword_groups:
        for n in names:
            low = n.lower()
            if all(kw in low for kw in group):
                return n
    return None


async def _quick_email_check() -> str:
    from .mcp import mcp_client
    if not mcp_client:
        return json.dumps({"error": "MCP not initialized"})
    tool = _find_mcp_tool(
        ("unread",),
        ("list", "mail"),
        ("list", "message"),
        ("inbox",),
    )
    if not tool:
        return json.dumps({"error": "no email tool found in MCP fleet"})
    return await mcp_client.call_tool(tool, {})


async def _quick_calendar(days_ahead: int = 1) -> str:
    from datetime import datetime, timedelta, timezone
    from .mcp import mcp_client
    if not mcp_client:
        return json.dumps({"error": "MCP not initialized"})
    tool = _find_mcp_tool(
        ("list", "event"),
        ("upcoming",),
        ("agenda",),
    )
    if not tool:
        return json.dumps({"error": "no calendar tool found in MCP fleet"})
    now = datetime.now(timezone.utc)
    args = {
        "timeMin": now.isoformat(),
        "timeMax": (now + timedelta(days=max(1, days_ahead))).isoformat(),
    }
    return await mcp_client.call_tool(tool, args)


# ---------------------------------------------------------------------------
# Prompt management tools
# ---------------------------------------------------------------------------

async def _list_prompts() -> str:
    names = list_templates()
    return json.dumps({"prompts": names})


async def _show_prompt(name: str) -> str:
    try:
        content = read_raw(name)
    except FileNotFoundError:
        return json.dumps({"error": f"Prompt '{name}' not found. Available: {list_templates()}"})

    if _post_callback:
        asyncio.create_task(_post_callback(f"**Prompt: `{name}`**\n\n{content}"))

    summary = content[:300].replace("\n", " ")
    if len(content) > 300:
        summary += "..."
    return json.dumps({"name": name, "length": len(content), "summary": summary})


async def _edit_prompt(name: str, instruction: str, session_key: str = "") -> str:
    if not _anthropic_client:
        return json.dumps({"error": "Anthropic client not initialized"})
    state = _state_for(session_key)
    state.claude_calls += 1
    if state.claude_calls > config.per_session_claude_calls_max:
        return json.dumps({"error": f"Per-session Claude call limit ({config.per_session_claude_calls_max}) reached"})

    try:
        current = read_raw(name)
    except FileNotFoundError:
        return json.dumps({"error": f"Prompt '{name}' not found. Available: {list_templates()}"})

    response = await asyncio.to_thread(
        _anthropic_client.messages.create,
        model=config.claude_model,
        system=(
            "You are editing a prompt template. Return ONLY the complete "
            "edited content. Do not wrap in markdown code fences. Do not "
            "add commentary before or after. Just the updated prompt text."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Current prompt:\n\n{current}\n\n---\n\n"
                f"Edit instruction: {instruction}\n\n"
                "Return the complete updated prompt."
            ),
        }],
        max_tokens=4096,
    )

    new_content = response.content[0].text

    if response.usage:
        cost = _estimate_cost(response.usage.input_tokens, response.usage.output_tokens)
        log_event("edit_prompt", {"name": name}, instruction[:200], 0, "", cost)

    save_template(name, new_content, origin="user")

    if _post_callback:
        asyncio.create_task(_post_callback(
            f"**Updated prompt: `{name}`**\n\n{new_content}"
        ))

    needs_reload = name == "gemini_system"
    return json.dumps({
        "ok": True,
        "name": name,
        "needs_reload": needs_reload,
        "message": (
            f"Prompt '{name}' updated. "
            + ("Call reload_prompts to apply changes to your system prompt."
               if needs_reload else "Changes take effect on next use.")
        ),
    })


async def _rollback_prompt(name: str, version: int) -> str:
    try:
        content = rollback_template(name, version)
    except (FileNotFoundError, ValueError) as e:
        return json.dumps({"error": str(e)})

    if _post_callback:
        asyncio.create_task(_post_callback(
            f"**Rolled back prompt: `{name}` to v{version}**\n\n{content}"
        ))

    needs_reload = name == "gemini_system"
    return json.dumps({
        "ok": True,
        "name": name,
        "restored_version": version,
        "needs_reload": needs_reload,
        "message": (
            f"Prompt '{name}' rolled back to version {version}. "
            + ("Call reload_prompts to apply changes to your system prompt."
               if needs_reload else "Changes take effect on next use.")
        ),
    })


async def _prompt_versions(name: str) -> str:
    versions = prompts_get_versions(name)
    if not versions:
        return json.dumps({"name": name, "versions": [], "message": "No version history yet."})
    return json.dumps({"name": name, "versions": versions})


async def _reload_prompts() -> str:
    global _model_costs_cache
    prompts_clear_cache()
    _model_costs_cache = None

    if _reconnect_callback:
        await _reconnect_callback()

    if _alert_callback:
        asyncio.create_task(_alert_callback("Prompts reloaded. Gemini session reconnected."))

    return json.dumps({"ok": True, "message": "Prompt cache cleared. Session reconnected."})


# ---------------------------------------------------------------------------
# Mac dictation tools (clipboard + paste into focused app)
# ---------------------------------------------------------------------------

async def _get_focused_app() -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e",
        'tell application "System Events" to get name of first process whose frontmost is true',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    name = stdout.decode().strip()

    proc2 = await asyncio.create_subprocess_exec(
        "osascript", "-e",
        'tell application "System Events" to get bundle identifier of first process whose frontmost is true',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout2, _ = await proc2.communicate()
    bundle_id = stdout2.decode().strip()

    return json.dumps({"name": name, "bundle_id": bundle_id})


async def _focus_app(app_name: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e",
        f'tell application "{app_name}" to activate',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        return json.dumps({"error": f"Failed to activate {app_name}: {stderr.decode().strip()}"})
    await asyncio.sleep(0.3)
    return json.dumps({"ok": True, "activated": app_name})


async def _dictate_into_focused_app(text: str) -> str:
    proc_name = await asyncio.create_subprocess_exec(
        "osascript", "-e",
        'tell application "System Events" to get name of first process whose frontmost is true',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc_name.communicate()
    app_name = stdout.decode().strip()

    proc_copy = await asyncio.create_subprocess_exec(
        "pbcopy", stdin=asyncio.subprocess.PIPE,
    )
    await proc_copy.communicate(input=text.encode("utf-8"))

    proc_paste = await asyncio.create_subprocess_exec(
        "osascript", "-e",
        'tell application "System Events" to keystroke "v" using command down',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc_paste.communicate()
    if proc_paste.returncode != 0:
        return json.dumps({"error": f"Paste failed: {stderr.decode().strip()}"})

    return json.dumps({"pasted_into": app_name, "chars": len(text)})


# ---------------------------------------------------------------------------
# Cursor IDE remote-control tools
#
# These are Aria's eyes and hands on the other Cursor windows the user
# opened manually. Because the user is away from the workstation, every
# input that touches a Cursor window has to be a tool Aria can call.
#
# Read tools (free, local-only):
#   list_cursor_windows       - enumerate open Cursor.app windows
#   read_cursor_window        - last N turns from the latest transcript JSONL
#   list_cursor_plans         - recently modified plan files under ~/.cursor/plans
#
# Write tools (free, local-only, brittle around UI scripting):
#   focus_cursor_window       - bring a specific Cursor window to front
#   send_to_cursor_chat       - focus + open chat sidebar + paste + send
#   keystroke_to_cursor_window - send arbitrary keystrokes (escape hatch)
#   screenshot_cursor_window  - capture the focused window for visual context
#   approve_cursor_plan       - paste "approve and proceed" into chat
#   reject_cursor_plan        - paste "cancel" into chat
# ---------------------------------------------------------------------------


async def _run_osascript(script: str, timeout: float = 6.0) -> tuple[int, str, str]:
    """Run an AppleScript and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"osascript timed out after {timeout}s"
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _resolve_cursor_target(
    project: str | None,
    workspace_root: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve (project, workspace_root) to (search_substring, registered_path).

    The substring is what we'll match in Cursor window titles via osascript.
    The path is the on-disk cwd used for transcript JSONL lookups.

    Resolution order:
      1. Registered short name (`project in PROJECT_REGISTRY`).
      2. Absolute path passed as `project` that exists on disk.
      3. `workspace_root` fallback when `project` is empty or unresolved —
         use the basename as the title substring and the path verbatim.
      4. Raw `project` string as substring with no path (osascript can
         still try to match a title, but tools needing the cwd return
         "Unknown project").

    Step 3 lets Aria's tool calls succeed when the only handle she has
    is the `workspace_root` an observer event carried (e.g. ad-hoc
    Cursor windows that aren't in projects/registry.md).
    """
    if project and project in PROJECT_REGISTRY:
        return project, PROJECT_REGISTRY[project]
    if project and "/" in project and os.path.isdir(project):
        return os.path.basename(project.rstrip("/")), project
    if workspace_root:
        norm = workspace_root.rstrip("/")
        if os.path.isdir(norm):
            return os.path.basename(norm), norm
    if project:
        return project, None
    return None, None


async def _list_cursor_windows() -> str:
    """Enumerate open Cursor.app windows.

    Returns JSON `{"windows": [{"title": "...", "matches_project": "name|null"}, ...]}`.
    Empty list if Cursor isn't running or has no windows.
    """
    script = (
        'tell application "System Events"\n'
        '  if not (exists process "Cursor") then return ""\n'
        '  set out to ""\n'
        '  repeat with w in (every window of process "Cursor")\n'
        '    set out to out & (name of w) & linefeed\n'
        '  end repeat\n'
        '  return out\n'
        'end tell'
    )
    rc, stdout, stderr = await _run_osascript(script)
    if rc != 0:
        return json.dumps({"error": f"osascript failed: {stderr.strip()[:300]}"})

    titles = [t.strip() for t in stdout.splitlines() if t.strip()]
    out = []
    for t in titles:
        matched_name = None
        for name, path in PROJECT_REGISTRY.items():
            import os as _os
            base = _os.path.basename(path.rstrip("/"))
            if name and (name in t or (base and base in t)):
                matched_name = name
                break
        out.append({"title": t, "matches_project": matched_name})
    return json.dumps({"windows": out, "count": len(out)})


async def _read_cursor_window(
    project: str | None = None,
    n_turns: int = 5,
    workspace_root: str | None = None,
) -> str:
    """Return the last N turns of the most recent transcript for a project,
    plus any plan files modified in the last 10 minutes.

    Accepts either a registered project name, an absolute path as `project`,
    or a `workspace_root` (absolute path). `workspace_root` is the ground
    truth that Cursor hooks deliver — pass it when you have it (e.g. from
    a recently paged cursor event) so resolution succeeds even for
    ad-hoc windows that aren't in projects/registry.md.

    Falls back gracefully if Cursor has no data on disk for the project yet.
    """
    from .cursor_external import read_last_n_turns, list_recent_plans

    _, cwd = _resolve_cursor_target(project, workspace_root)
    if not cwd:
        if project and project.startswith("/"):
            cwd = project
        elif workspace_root and workspace_root.startswith("/"):
            cwd = workspace_root
        else:
            return json.dumps({
                "error": (
                    f"Unknown project: {project!r}. Known: {list(PROJECT_REGISTRY.keys())}. "
                    "Pass workspace_root with the absolute Cursor cwd if you have it."
                ),
            })

    n = max(1, min(int(n_turns) if n_turns else 5, 25))
    turns = read_last_n_turns(cwd, n=n)
    plans = list_recent_plans(max_age_sec=600, limit=5)
    return json.dumps({
        "project": project,
        "workspace_root": workspace_root or cwd,
        "cwd": cwd,
        "turns_returned": len(turns),
        "turns": turns,
        "recent_plans": plans,
    })


async def _list_cursor_plans(max_age_minutes: int = 60) -> str:
    """List recently modified Cursor plan files across all windows."""
    from .cursor_external import list_recent_plans
    max_age = max(1, int(max_age_minutes)) * 60
    plans = list_recent_plans(max_age_sec=max_age, limit=20)
    return json.dumps({"plans": plans, "count": len(plans), "max_age_seconds": max_age})


async def _focus_cursor_window(project: str) -> str:
    """Bring the Cursor window whose title contains `project` to the front.

    Forces focus aggressively: activates Cursor.app, sets the process
    frontmost via System Events (more reliable than `activate` alone when
    another app currently owns focus), raises the matching window, then
    verifies the frontmost process is actually Cursor. If verification
    fails we re-issue the activation once before giving up.
    """
    search, _path = _resolve_cursor_target(project)
    if not search:
        return json.dumps({"error": f"Cannot resolve project: {project!r}"})

    safe = search.replace('"', '').replace('\\', '')
    script = (
        f'set target to "{safe}"\n'
        'try\n'
        '  tell application "Cursor" to activate\n'
        'end try\n'
        'delay 0.25\n'
        'tell application "System Events"\n'
        '  if not (exists process "Cursor") then return "ERR: Cursor not running"\n'
        '  tell process "Cursor"\n'
        '    try\n'
        '      set frontmost to true\n'
        '    end try\n'
        '    set hits to {}\n'
        '    repeat with w in (every window)\n'
        '      if (name of w) contains target then\n'
        '        try\n'
        '          perform action "AXRaise" of w\n'
        '        end try\n'
        '        set end of hits to (name of w)\n'
        '      end if\n'
        '    end repeat\n'
        '    if (count of hits) = 0 then\n'
        '      return "NOMATCH"\n'
        '    end if\n'
        '  end tell\n'
        '  delay 0.2\n'
        '  set frontApp to name of first process whose frontmost is true\n'
        '  if frontApp is not "Cursor" then\n'
        '    -- Try once more, forcefully\n'
        '    try\n'
        '      tell application "Cursor" to activate\n'
        '    end try\n'
        '    tell process "Cursor" to set frontmost to true\n'
        '    delay 0.2\n'
        '    set frontApp to name of first process whose frontmost is true\n'
        '  end if\n'
        '  return (item 1 of hits) & "|" & frontApp\n'
        'end tell'
    )
    rc, stdout, stderr = await _run_osascript(script, timeout=6.0)
    if rc != 0:
        return json.dumps({"error": f"osascript failed: {stderr.strip()[:300]}"})
    out = stdout.strip()
    if out == "NOMATCH":
        return json.dumps({
            "ok": False,
            "matched": None,
            "search": search,
            "note": (
                "No Cursor window title contained the search substring. Call "
                "list_cursor_windows to see what's open."
            ),
        })
    if out.startswith("ERR:"):
        return json.dumps({"error": out[5:].strip()})
    matched, _, front = out.partition("|")
    if front and front != "Cursor":
        return json.dumps({
            "ok": False,
            "matched": matched,
            "search": search,
            "front_app": front,
            "note": (
                f"AppleScript raised the window but {front!r} is still frontmost. "
                "The user (or another app) is holding focus. Aria cannot send "
                "keystrokes to Cursor right now. Wait and retry, or ask Corbin "
                "to release focus."
            ),
        })
    await asyncio.sleep(0.25)
    return json.dumps({"ok": True, "matched": matched, "search": search, "front_app": front})


async def _keystroke_to_cursor_window(project: str, keys: str, modifiers: str | None = None) -> str:
    """Send a raw AppleScript keystroke to a Cursor window after focusing it.

    `keys` is the literal text to send (System Events `keystroke "..."`).
    `modifiers` is a comma-separated subset of {command, control, option, shift}.
    For special keys like Return/Tab/Escape, prefer `keystroke_to_cursor_window`
    with the named key wrapped in AppleScript: callers can use the dedicated
    helper tools (send_to_cursor_chat, etc.) for the common cases.
    """
    focus_result = await _focus_cursor_window(project)
    try:
        parsed = json.loads(focus_result)
    except Exception:
        return focus_result
    if not parsed.get("ok"):
        return focus_result

    await asyncio.sleep(0.15)

    safe = keys.replace('\\', '\\\\').replace('"', '\\"')
    using_clause = ""
    if modifiers:
        mods = [m.strip() for m in modifiers.split(",") if m.strip()]
        valid = {"command", "control", "option", "shift"}
        bad = [m for m in mods if m not in valid]
        if bad:
            return json.dumps({"error": f"Invalid modifiers: {bad}. Allowed: {sorted(valid)}"})
        if mods:
            using_clause = " using {" + ", ".join(f"{m} down" for m in mods) + "}"

    script = f'tell application "System Events" to keystroke "{safe}"{using_clause}'
    rc, _stdout, stderr = await _run_osascript(script, timeout=4.0)
    if rc != 0:
        return json.dumps({"error": f"keystroke failed: {stderr.strip()[:300]}"})
    return json.dumps({"ok": True, "sent": keys, "modifiers": modifiers or ""})


async def _send_to_cursor_chat(
    project: str,
    message: str,
    new_agent: bool = True,
    send_delay_sec: float = 0.7,
    verify_timeout_sec: float = 0.0,
) -> str:
    """Type a message into the Cursor chat input for `project` and send it.

    Folds focus + open-composer + paste + send into ONE atomic AppleScript
    invocation. Between subprocess calls another app can reclaim focus
    (Finder, the terminal, anything in the user's window order), so we
    keep keystrokes inside a single script that re-verifies frontmost
    immediately before each keystroke.

    `verify_timeout_sec` (default 0 = disabled). When > 0, we poll the
    Cursor JSONL transcripts for that project after the send and report
    `verified_landed=True` if any transcript file mtime advances. This
    is a hint, not a guarantee — Cursor can take 8-20s to start writing
    after receiving keystrokes. The recommended pattern for callers is:
    leave verification disabled, then call read_cursor_window after a
    short delay (5-15s) and inspect the latest turn to confirm the agent
    received your message. Re-sending blindly on a false-negative verify
    risks double-firing the same task.

    Sequence inside the script:
      1. Activate Cursor.app + set process frontmost.
      2. Find the window whose title contains the project substring.
      3. AXRaise that window.
      4. Re-verify Cursor is frontmost. If not, retry once.
      5. Keystroke Cmd+I (new agent composer) or Cmd+L (existing chat).
      6. Re-verify frontmost again before paste.
      7. Cmd+V to paste (clipboard already loaded by Python).
      8. Wait send_delay_sec.
      9. Press Return to send.

    Returns ok=True with the matched window title, OR ok=False with a
    note explaining where focus got stolen.
    """
    search, path = _resolve_cursor_target(project)
    if not search:
        return json.dumps({"error": f"Cannot resolve project: {project!r}"})

    from .cursor_external import cursor_project_data_dir

    def _latest_jsonl_mtime(transcripts_root: str) -> float:
        """Max mtime across every <sid>.jsonl under the agent-transcripts dir.

        We need FILE mtimes here, not directory mtimes — subdir mtimes only
        change when files are added/removed, but JSONLs are appended to in
        place. Appending to a file updates the file's mtime but NOT the
        parent dir's.
        """
        latest = 0.0
        try:
            for entry in os.listdir(transcripts_root):
                sub = os.path.join(transcripts_root, entry)
                if not os.path.isdir(sub):
                    continue
                for fname in os.listdir(sub):
                    if not fname.endswith(".jsonl"):
                        continue
                    p = os.path.join(sub, fname)
                    try:
                        m = os.path.getmtime(p)
                    except OSError:
                        continue
                    if m > latest:
                        latest = m
        except OSError:
            return 0.0
        return latest

    transcripts_dir: str | None = None
    pre_send_mtime: float = 0.0
    if path and os.path.isdir(path):
        proj_data = cursor_project_data_dir(path)
        transcripts_dir = os.path.join(proj_data, "agent-transcripts")
        if os.path.isdir(transcripts_dir):
            pre_send_mtime = _latest_jsonl_mtime(transcripts_dir)

    proc_copy = await asyncio.create_subprocess_exec(
        "pbcopy", stdin=asyncio.subprocess.PIPE,
    )
    await proc_copy.communicate(input=message.encode("utf-8"))

    open_key = "i" if new_agent else "l"
    open_label = "Cmd+I (new agent composer)" if new_agent else "Cmd+L (AI chat sidebar)"
    safe = search.replace('"', '').replace('\\', '')
    delay_ms = int(max(200, float(send_delay_sec) * 1000))

    script = f"""
set target to "{safe}"
set openKey to "{open_key}"
try
  tell application "Cursor" to activate
end try
delay 0.25
tell application "System Events"
  if not (exists process "Cursor") then return "ERR: Cursor not running"
  tell process "Cursor"
    try
      set frontmost to true
    end try
    set hits to {{}}
    repeat with w in (every window)
      if (name of w) contains target then
        try
          perform action "AXRaise" of w
        end try
        set end of hits to (name of w)
      end if
    end repeat
    if (count of hits) = 0 then return "NOMATCH"
  end tell
  delay 0.25
  if (name of first process whose frontmost is true) is not "Cursor" then
    try
      tell application "Cursor" to activate
    end try
    tell process "Cursor" to set frontmost to true
    delay 0.25
  end if
  set frontBeforeOpen to name of first process whose frontmost is true
  if frontBeforeOpen is not "Cursor" then
    return "ERR: focus stolen before open by " & frontBeforeOpen
  end if
  keystroke openKey using {{command down}}
  delay 0.6
  set frontBeforePaste to name of first process whose frontmost is true
  if frontBeforePaste is not "Cursor" then
    return "ERR: focus stolen before paste by " & frontBeforePaste
  end if
  keystroke "v" using {{command down}}
  delay 0.{delay_ms // 100}
  set frontBeforeSend to name of first process whose frontmost is true
  if frontBeforeSend is not "Cursor" then
    return "ERR: focus stolen before send by " & frontBeforeSend
  end if
  key code 36
end tell
return "OK|" & (item 1 of hits)
"""
    rc, stdout, stderr = await _run_osascript(script, timeout=15.0)
    if rc != 0:
        return json.dumps({"error": f"osascript failed: {stderr.strip()[:400]}"})
    out = stdout.strip()
    if out == "NOMATCH":
        return json.dumps({
            "ok": False,
            "matched": None,
            "search": search,
            "note": (
                "No Cursor window title contained the search substring. Call "
                "list_cursor_windows to see what's open."
            ),
        })
    if out.startswith("ERR:"):
        return json.dumps({
            "ok": False,
            "search": search,
            "note": out[4:].strip(),
            "open_method": open_label,
        })
    if not out.startswith("OK|"):
        return json.dumps({"ok": False, "raw": out[:200]})
    matched = out[3:]

    landed = False
    landed_via = ""
    if transcripts_dir and verify_timeout_sec > 0:
        deadline = time.monotonic() + float(verify_timeout_sec)
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            if not os.path.isdir(transcripts_dir):
                continue
            latest = _latest_jsonl_mtime(transcripts_dir)
            if latest > pre_send_mtime + 0.5:
                landed = True
                landed_via = "transcript_mtime_advanced"
                break

    return json.dumps({
        "ok": True,
        "matched": matched,
        "chars_sent": len(message),
        "open_method": open_label,
        "verified_landed": landed,
        "verify_signal": landed_via or ("no transcript directory observed" if not transcripts_dir else "timed out waiting for mtime change"),
    })


async def _screenshot_cursor_window(project: str, save_path: str | None = None) -> str:
    """Capture the focused Cursor window to a PNG. Returns the file path.

    Uses `screencapture -l` (by window ID) — needs the window to be raised
    first, which `_focus_cursor_window` does. If `save_path` is omitted, a
    timestamped path under `data/screenshots/` is created.
    """
    import os as _os
    focus_result = await _focus_cursor_window(project)
    try:
        parsed = json.loads(focus_result)
    except Exception:
        return focus_result
    if not parsed.get("ok"):
        return focus_result
    await asyncio.sleep(0.25)

    if save_path is None:
        out_dir = _os.path.join(config.data_dir, "screenshots")
        _os.makedirs(out_dir, exist_ok=True)
        ts = int(time.time())
        safe_proj = "".join(c for c in project if c.isalnum() or c in "._-")[:40] or "win"
        save_path = _os.path.join(out_dir, f"cursor-{safe_proj}-{ts}.png")

    proc = await asyncio.create_subprocess_exec(
        "screencapture", "-o", "-x", save_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        return json.dumps({"error": f"screencapture failed: {stderr.decode().strip()[:200]}"})

    return json.dumps({
        "ok": True,
        "path": save_path,
        "size_bytes": _os.path.getsize(save_path) if _os.path.exists(save_path) else 0,
    })


async def _approve_cursor_plan(project: str, note: str = "") -> str:
    """Tell the focused Cursor plan-mode agent to proceed.

    Sends "Approve and proceed." (optionally with an appended note) into
    the chat input. This is the resilient path: it does not depend on the
    plan-approve button's accessibility hierarchy, which shifts between
    Cursor releases. Aria will see the agent leave plan mode in the
    transcript JSONL.
    """
    message = "Approve and proceed."
    if note:
        message = f"{message} {note}"
    return await _send_to_cursor_chat(project, message)


async def _reject_cursor_plan(project: str, reason: str = "") -> str:
    """Tell the focused Cursor plan-mode agent NOT to proceed."""
    if reason:
        message = f"Stop. Do not proceed with this plan. {reason}"
    else:
        message = "Stop. Do not proceed with this plan."
    return await _send_to_cursor_chat(project, message)
