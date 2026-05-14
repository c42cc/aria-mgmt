"""Tool implementations dispatched by Gemini function calling."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

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


def _get_model_costs() -> tuple[float, float]:
    """Read cost_per_m_input/output for the active Claude model from models.yaml."""
    import yaml
    try:
        with open(config.models_config) as f:
            data = yaml.safe_load(f)
        for spec in (data or {}).get("models", {}).values():
            if spec.get("model_id") == config.claude_model:
                return spec.get("cost_per_m_input", 15.0), spec.get("cost_per_m_output", 75.0)
    except Exception:
        pass
    return 15.0, 75.0


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
_transcript_provider: Callable[[], list[dict[str, str]]] | None = None
_cancel = False
_session_claude_calls = 0
_session_cursor_runs = 0

PROJECT_REGISTRY: dict[str, str] = {}


def init_tools(
    cursor_bridge: CursorBridge,
    post_callback: Callable[..., Coroutine] | None = None,
    alert_callback: Callable[..., Coroutine] | None = None,
    thread_callback: Callable[..., Coroutine] | None = None,
    cursor_event_callback: Callable[..., Coroutine] | None = None,
    reconnect_callback: Callable[..., Coroutine] | None = None,
) -> None:
    """Initialize tool dependencies. Call once at bot startup."""
    global _anthropic_client, _cursor_bridge
    global _post_callback, _alert_callback, _thread_callback, _cursor_event_callback
    global _reconnect_callback

    _anthropic_client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    _cursor_bridge = cursor_bridge
    _post_callback = post_callback
    _alert_callback = alert_callback
    _thread_callback = thread_callback
    _cursor_event_callback = cursor_event_callback
    _reconnect_callback = reconnect_callback
    _load_project_registry()


def set_transcript_provider(provider: Callable[[], list[dict[str, str]]]) -> None:
    """Inject the transcript source. Called by bot.py after Gemini session is created."""
    global _transcript_provider
    _transcript_provider = provider


def set_cancel_flag(value: bool) -> None:
    global _cancel
    _cancel = value


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
        "cursor_status": _cursor_status,
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
    }
    handler = handlers.get(name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {name}"})

    spend = get_daily_spend()
    _free_tools = (
        "cursor_status", "recall", "cancel_current_task", "list_prompts",
        "show_prompt", "prompt_versions", "reload_prompts",
        "get_focused_app", "focus_app", "dictate_into_focused_app",
    )
    if spend >= config.daily_spend_cap_usd and name not in _free_tools:
        return json.dumps({"error": f"Daily spend cap (${config.daily_spend_cap_usd}) reached. Current: ${spend:.2f}"})

    transcript = _transcript_provider() if _transcript_provider else []
    session_key = args.get("session_key", "")

    start = time.monotonic()
    try:
        result = await handler(**args)
        duration_ms = int((time.monotonic() - start) * 1000)
        log_event(name, args, str(result)[:500], duration_ms)
        result_str = result if isinstance(result, str) else json.dumps(result)

        _emit_session_record(
            name, args, session_key, transcript,
            result_str, duration_ms, status="ok",
        )
        return result_str
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        log.exception("Tool call %s failed", name)
        error_result = json.dumps({"error": str(e)})
        _emit_session_record(
            name, args, session_key, transcript,
            error_result, duration_ms, status="error",
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
) -> None:
    """Write a session record and optionally fire the judge (EMIT layer)."""
    product = tool_to_product(tool_name)
    if product == "system":
        return

    inputs = {"args": args, "transcript": transcript}
    outputs = {"result": result[:10_000], "duration_ms": duration_ms, "status": status}

    record_id = record_session(
        session_key=session_key or "unknown",
        tool_name=tool_name,
        inputs=inputs,
        outputs=outputs,
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
    global _cancel, _session_claude_calls
    _cancel = False
    _session_claude_calls += 1
    if _session_claude_calls > config.per_session_claude_calls_max:
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

    response = _anthropic_client.messages.create(
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

    if _post_callback:
        asyncio.create_task(_post_callback(result_text))

    return result_text


async def _plan_with_claude_ucs(
    context: str,
    session_key: str,
    prompt_template: str = "planning",
) -> str:
    global _cancel, _session_claude_calls
    _cancel = False
    _session_claude_calls += 1
    if _session_claude_calls > config.per_session_claude_calls_max:
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
        post_callback=_post_callback,
        cancel_check=lambda: _cancel,
    )

    memories_for_history = mem_recall(context, limit=3)
    if memories_for_history:
        memory_context = "\n".join(f"- {m.get('memory', m.get('text', ''))}" for m in memories_for_history)
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
) -> dict[str, Any]:
    global _session_cursor_runs
    _session_cursor_runs += 1
    if _session_cursor_runs > config.per_session_cursor_runs_max:
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
# Do with Claude (MCP agent loop)
# ---------------------------------------------------------------------------

async def _do_with_claude(
    task: str,
    session_key: str = "",
) -> str:
    if config.ucs_enabled:
        return await _do_with_claude_ucs(task, session_key)
    return await _do_with_claude_legacy(task, session_key)


async def _do_with_claude_legacy(
    task: str,
    session_key: str = "",
) -> str:
    global _cancel
    _cancel = False
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

    messages: list[dict[str, Any]] = [{"role": "user", "content": memory_ctx + task}]

    max_iterations = config.do_with_claude_max_iterations
    iteration = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost = 0.0
    max_tokens_budget = 50000
    started_at = time.monotonic()
    started_at_iso = _now_iso()
    final_status = "completed"

    while iteration < max_iterations and not _cancel:
        iteration += 1

        response = _anthropic_client.messages.create(
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
            if _post_callback:
                asyncio.create_task(_post_callback(result))
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
            return result

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_result = await mcp_client.call_tool(
                block.name, dict(block.input) if block.input else {}, session_key=session_key,
            )
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(tool_result)[:4000],
            })

        messages.append({"role": "user", "content": tool_results})

        if total_output_tokens > max_tokens_budget:
            final_status = "token_budget"
            if _alert_callback:
                asyncio.create_task(_alert_callback(
                    f"do_with_claude token budget exceeded ({total_output_tokens} tokens)"
                ))
            break

    if _cancel:
        final_status = "cancelled"
    elif final_status != "token_budget":
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

    if _cancel:
        return "Task cancelled by user."

    partial = f"Task reached iteration limit ({max_iterations}). Partial progress made."
    if _alert_callback:
        asyncio.create_task(_alert_callback(partial))
    return partial


async def _do_with_claude_ucs(
    task: str,
    session_key: str = "",
) -> str:
    global _cancel
    _cancel = False

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
        post_callback=_post_callback,
        alert_callback=_alert_callback,
        cancel_check=lambda: _cancel,
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

async def _cancel_current_task() -> str:
    global _cancel
    _cancel = True
    if _alert_callback:
        asyncio.create_task(_alert_callback("Task cancelled via voice command."))
    return json.dumps({"ok": True, "cancelled": True})


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


async def _edit_prompt(name: str, instruction: str) -> str:
    if not _anthropic_client:
        return json.dumps({"error": "Anthropic client not initialized"})

    try:
        current = read_raw(name)
    except FileNotFoundError:
        return json.dumps({"error": f"Prompt '{name}' not found. Available: {list_templates()}"})

    response = _anthropic_client.messages.create(
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
    prompts_clear_cache()

    if _reconnect_callback:
        asyncio.create_task(_reconnect_callback())

    if _alert_callback:
        asyncio.create_task(_alert_callback("Prompts reloaded. Gemini session reconnecting."))

    return json.dumps({"ok": True, "message": "Prompt cache cleared. Session reconnecting."})


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
