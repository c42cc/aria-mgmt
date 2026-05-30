"""Tool implementations dispatched by Gemini function calling."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo

import anthropic

from .config import config
from .cursor_bridge import CursorBridge
from .db import (
    JUDGE_WORTHY_PRODUCTS,
    append_planning_history,
    get_active_cursor_sessions,
    get_daily_spend,
    get_planning_history,
    log_event,
    log_loop_execution,
    record_session,
    tool_to_product,
    upsert_cursor_session,
)
from .cursor_tools import (
    _cursor_agents,
    _cursor_read,
    _cursor_screenshot,
    _cursor_send,
    _cursor_spawn,
    _cursor_status_new,
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


# Maximum number of tier-X/I declines (timeouts or explicit no) allowed
# across an entire agent loop before we abort early and surface an
# explicit blocker. Independent of per-action repeats below.
_DECLINE_TOTAL_ABORT = 3
# Maximum number of declines for a single (tool, args) pair before we
# abort. The 42c.pw failure repeatedly retried htpasswd/openssl variants;
# 2 is enough headroom for one accidental decline + one retry.
_DECLINE_PER_ACTION_ABORT = 2


def _is_declined_result(result_str: str) -> bool:
    """True if `result_str` is the typed ERR_DECLINED envelope from src/mcp.py."""
    if not result_str:
        return False
    s = result_str.strip()
    if not s.startswith("{"):
        return False
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(obj, dict) and obj.get("_error_class") == "declined"


def _declined_reason(result_str: str) -> str:
    """Short reason from the ERR_DECLINED envelope, or 'declined' as fallback."""
    try:
        obj = json.loads(result_str)
        msg = str(obj.get("_message") or obj.get("_raw") or "declined")
        return msg[:300]
    except Exception:
        return "declined"


def _format_decline_blocker(
    tool_name: str, args: dict, reason: str, per_action_count: int, total_count: int
) -> str:
    """Build the user-facing blocker message for an early decline-driven abort.

    Names the exact command and tells the user how to approve. This is
    the "loud failure" your rules require — the prior behavior was to
    silently burn the iteration budget and emit a generic 'iteration
    limit' string.
    """
    args_preview = json.dumps(args, default=str)[:300]
    return (
        f"**Blocked: approval required for `{tool_name}`** "
        f"(declined {per_action_count}x, {total_count} total decline(s) this task).\n"
        f"Args: `{args_preview}`\n"
        f"Reason: {reason}\n"
        f"To unblock: reply `!ok <action_id>` (or react \u2705) on the next "
        f"confirmation card in #ucs-alerts, or say \"yes\" in voice. Then "
        f"send the task again. Stopping early so we don't burn the rest of "
        f"the iteration budget on retries."
    )


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


def _build_context(session_key: str = "") -> str:
    """Compose a deterministic `<context>` block for the agent's user message.

    P3: this block is recomputed every call. It supplies facts Aria
    historically guessed (date, today, primary mail source, what tools are
    actually available) so she never has to fetch them via a fallible
    upstream tool. Failures here are non-fatal — we always return at least
    the time line.
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


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    cost_in, cost_out = _get_model_costs()
    return (input_tokens / 1_000_000 * cost_in
            + output_tokens / 1_000_000 * cost_out)


_anthropic_client: anthropic.Anthropic | None = None
_cursor_bridge: CursorBridge | None = None
_post_callback: Callable[..., Coroutine] | None = None
_alert_callback: Callable[..., Coroutine] | None = None
_thread_callback: Callable[..., Coroutine] | None = None
_cursor_event_callback: Callable[..., Coroutine] | None = None
_reconnect_callback: Callable[..., Coroutine] | None = None
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
) -> None:
    """Initialize tool dependencies. Call once at bot startup."""
    global _anthropic_client, _cursor_bridge
    global _post_callback, _alert_callback, _thread_callback, _cursor_event_callback
    global _reconnect_callback
    global _discord_history_callback, _discord_threads_callback

    _anthropic_client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    _cursor_bridge = cursor_bridge
    _post_callback = post_callback
    _alert_callback = alert_callback
    _thread_callback = thread_callback
    _cursor_event_callback = cursor_event_callback
    _reconnect_callback = reconnect_callback
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


async def handle_tool_call(name: str, args: dict) -> str:
    """Dispatch a tool call by name. Returns JSON string."""
    handlers = {
        "plan_with_claude": _plan_with_claude,
        "build_with_cursor": _build_with_cursor,
        "query_cursor": _query_cursor,
        "cursor_status": _cursor_status_new,
        "do_with_claude": _do_with_claude,
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
        # Discord text-history tools — read-only.
        "discord_recent_messages": _discord_recent_messages,
        "discord_list_threads": _discord_list_threads,
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
        "discord_recent_messages", "discord_list_threads",
    )
    if spend >= config.daily_spend_cap_usd and name not in _free_tools:
        return json.dumps({"error": f"Daily spend cap (${config.daily_spend_cap_usd}) reached. Current: ${spend:.2f}"})

    transcript = _transcript_provider() if _transcript_provider else []
    session_key = args.get("session_key", "")
    state = _state_for(session_key)
    state.last_tool_trace = None

    start = time.monotonic()
    try:
        result = await handler(**args)
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

    record_id = record_session(
        session_key=session_key or "unknown",
        tool_name=tool_name,
        inputs=inputs,
        outputs=outputs,
        context=context,
    )

    if record_id and product in JUDGE_WORTHY_PRODUCTS:
        _maybe_judge(record_id, product)


def _maybe_judge(record_id: int, product: str) -> None:
    """Fire-and-forget correctness judge on a session record."""
    try:
        from .judge import evaluate_record
        asyncio.create_task(_judge_and_alert(record_id, product))
    except Exception:
        log.debug("Judge not available, skipping evaluation", exc_info=True)


async def _judge_and_alert(record_id: int, product: str) -> None:
    """Run the judge and alert on failures."""
    from .judge import evaluate_record
    verdict = await evaluate_record(record_id, product)
    if verdict and verdict.verdict == "failed" and _alert_callback:
        reasons = "; ".join(verdict.reasons[:3]) if verdict.reasons else "no details"
        await _alert_callback(
            f"**Correctness FAILED** [{product}] score={verdict.score:.2f}\n{reasons}"
        )


# ---------------------------------------------------------------------------
# Plan with Claude
# ---------------------------------------------------------------------------

async def _plan_with_claude(
    context: str,
    session_key: str,
    prompt_template: str = "planning",
) -> str:
    if config.ucs_enabled:
        return await _plan_with_claude_ucs(context, session_key, prompt_template)
    return await _plan_with_claude_legacy(context, session_key, prompt_template)


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


async def _plan_with_claude_ucs(
    context: str,
    session_key: str,
    prompt_template: str = "planning",
) -> str:
    state = _state_for(session_key)
    state.cancel = False
    state.claude_calls += 1
    if state.claude_calls > config.per_session_claude_calls_max:
        return json.dumps({"error": f"Per-session Claude call limit ({config.per_session_claude_calls_max}) reached"})

    from .ucs import get_loop

    history = get_planning_history(session_key)
    memories = mem_recall(context, limit=3)

    started_at_iso = _now_iso()
    result = await get_loop().execute_planning(
        context=context,
        session_key=session_key,
        prompt_template=prompt_template,
        memories=memories if memories else None,
        history=history if history else None,
        cancel_check=lambda: state.cancel,
    )

    if memories:
        memory_context = "\n".join(f"- {m.get('memory', m.get('text', ''))}" for m in memories)
        history_context = f"Relevant memories:\n{memory_context}\n\n{context}"
    else:
        history_context = context
    append_planning_history(session_key, "user", history_context)
    append_planning_history(session_key, "assistant", result.text)

    log_event("plan_with_claude", {"session_key": session_key}, result.text[:200], 0, session_key, result.total_cost)
    log_loop_execution(
        tool_name="plan_with_claude",
        session_key=session_key,
        prompt_template=prompt_template,
        model_id=result.model_id,
        routing_path="ucs",
        tokens_in=result.total_tokens_in,
        tokens_out=result.total_tokens_out,
        cost_usd=result.total_cost,
        latency_ms=result.latency_ms,
        iterations=result.iterations,
        status=result.status,
        context_truncated=result.context_truncated,
        turns_dropped=result.turns_dropped,
        started_at=started_at_iso,
    )

    return result.text


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

async def _do_with_claude(
    task: str,
    session_key: str = "",
) -> str:
    lock = _agent_lock_for(session_key or "global")
    if lock.locked():
        return json.dumps({"error": "An agent loop is already running for this session. Wait for it to finish or use !stop."})
    async with lock:
        if config.ucs_enabled:
            return await _do_with_claude_ucs(task, session_key)
        return await _do_with_claude_legacy(task, session_key)


async def _do_with_claude_legacy(
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
        tools = mcp_client.list_tools_anthropic()
    except ImportError:
        return json.dumps({"error": "MCP module not available"})

    system_prompt = load_template("do_with_claude_system")

    memories = mem_recall(task, limit=3)
    memory_ctx = ""
    if memories:
        memory_ctx = "Relevant memories:\n" + "\n".join(
            f"- {m.get('memory', m.get('text', ''))}" for m in memories
        ) + "\n\n"

    context_block = _build_context(session_key)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": context_block + memory_ctx + task}
    ]

    max_iterations = config.do_with_claude_max_iterations
    iteration = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost = 0.0
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

    # Decline accounting — abort early so we don't burn the iteration
    # budget retrying tier-X/I commands the user is never going to approve.
    # 42c.pw failure mode: 11 declined execute_command variants × 30 iters
    # × $7+/iter with no user-facing report.
    decline_total = 0
    decline_per_action: dict[str, int] = {}
    decline_blocker: str | None = None

    while iteration < max_iterations + ground_check_retries and not state.cancel:
        iteration += 1

        response = await asyncio.to_thread(
            _anthropic_client.messages.create,
            model=config.claude_model,
            system=system_prompt,
            messages=messages,
            tools=tools,
            max_tokens=4096,
        )

        usage = response.usage
        if usage:
            total_input_tokens += usage.input_tokens
            total_output_tokens += usage.output_tokens
            cost = _estimate_cost(usage.input_tokens, usage.output_tokens)
            total_cost += cost
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
                        "content": _ground_check_user_message(violations),
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
                tool_result = await mcp_client.call_tool(
                    block.name, tool_args, session_key=session_key,
                )
                result_str = str(tool_result)
                called_tools[dedup_key] = (1, result_str)

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

            if _is_declined_result(result_str):
                decline_total += 1
                per_count = decline_per_action.get(dedup_key, 0) + 1
                decline_per_action[dedup_key] = per_count
                log.warning(
                    "tier-X/I declined: %s args=%s (per-action=%d, total=%d) session=%s",
                    block.name, str(tool_args)[:200], per_count, decline_total,
                    session_key,
                )
                if (
                    per_count >= _DECLINE_PER_ACTION_ABORT
                    or decline_total >= _DECLINE_TOTAL_ABORT
                ):
                    decline_blocker = _format_decline_blocker(
                        block.name, tool_args,
                        _declined_reason(result_str),
                        per_count, decline_total,
                    )
                    break

        messages.append({"role": "user", "content": tool_results})

        if decline_blocker is not None:
            final_status = "declined_abort"
            if _alert_callback:
                asyncio.create_task(_alert_callback(decline_blocker))
            break

        if total_output_tokens > max_tokens_budget:
            final_status = "token_budget"
            if _alert_callback:
                asyncio.create_task(_alert_callback(
                    f"do_with_claude token budget exceeded ({total_output_tokens} tokens)"
                ))
            break

    if state.cancel:
        final_status = "cancelled"
    elif final_status not in ("token_budget", "declined_abort"):
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

    state.last_tool_trace = _cap_trace_size(tool_trace) or None

    if state.cancel:
        return "Task cancelled by user."

    if decline_blocker is not None:
        return decline_blocker

    partial = (
        f"Task reached iteration limit ({max_iterations}). Partial progress made."
    )
    if decline_total > 0:
        partial += (
            f"\n\nNote: {decline_total} tier-X/I command(s) were declined "
            f"during this task — approval was required but never granted. "
            f"That likely prevented the work from completing. Reply "
            f"`!ok <action_id>` to the next confirmation card in #ucs-alerts."
        )
    if _alert_callback:
        asyncio.create_task(_alert_callback(partial))
    return partial


async def _do_with_claude_ucs(
    task: str,
    session_key: str = "",
) -> str:
    state = _state_for(session_key)
    state.cancel = False

    try:
        from .mcp import mcp_client
        if not mcp_client:
            return json.dumps({"error": "MCP client not initialized"})
    except ImportError:
        return json.dumps({"error": "MCP module not available"})

    from .ucs import get_loop

    memories = mem_recall(task, limit=3)
    started_at_iso = _now_iso()

    result = await get_loop().execute_agent(
        task=task,
        session_key=session_key,
        memories=memories if memories else None,
        mcp_client=mcp_client,
        alert_callback=_alert_callback,
        cancel_check=lambda: state.cancel,
    )

    log_loop_execution(
        tool_name="do_with_claude",
        session_key=session_key,
        prompt_template="do_with_claude_system",
        model_id=result.model_id,
        routing_path="ucs",
        tokens_in=result.total_tokens_in,
        tokens_out=result.total_tokens_out,
        cost_usd=result.total_cost,
        latency_ms=result.latency_ms,
        iterations=result.iterations,
        status=result.status,
        context_truncated=result.context_truncated,
        turns_dropped=result.turns_dropped,
        started_at=started_at_iso,
    )

    state.last_tool_trace = _cap_trace_size(result.tool_trace) if result.tool_trace else None
    return result.text


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------

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
