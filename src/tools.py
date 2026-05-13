"""Tool implementations dispatched by Gemini function calling."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Coroutine

import anthropic

from .config import config
from .cursor_bridge import CursorBridge
from .db import (
    append_planning_history,
    get_active_cursor_sessions,
    get_daily_spend,
    get_planning_history,
    log_event,
    upsert_cursor_session,
)
from .memory import recall as mem_recall, remember as mem_remember, forget as mem_forget
from .prompts import clear_cache as prompts_clear_cache, list_templates, load_template, read_raw, save_template

log = logging.getLogger(__name__)

CLAUDE_INPUT_COST_PER_M = 15.0
CLAUDE_OUTPUT_COST_PER_M = 75.0


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000 * CLAUDE_INPUT_COST_PER_M
            + output_tokens / 1_000_000 * CLAUDE_OUTPUT_COST_PER_M)


_anthropic_client: anthropic.Anthropic | None = None
_cursor_bridge: CursorBridge | None = None
_post_callback: Callable[..., Coroutine] | None = None
_alert_callback: Callable[..., Coroutine] | None = None
_thread_callback: Callable[..., Coroutine] | None = None
_cursor_event_callback: Callable[..., Coroutine] | None = None
_reconnect_callback: Callable[..., Coroutine] | None = None
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
        "reload_prompts": _reload_prompts,
    }
    handler = handlers.get(name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {name}"})

    spend = get_daily_spend()
    _free_tools = ("cursor_status", "recall", "cancel_current_task", "list_prompts", "show_prompt", "reload_prompts")
    if spend >= config.daily_spend_cap_usd and name not in _free_tools:
        return json.dumps({"error": f"Daily spend cap (${config.daily_spend_cap_usd}) reached. Current: ${spend:.2f}"})

    start = time.monotonic()
    try:
        result = await handler(**args)
        duration_ms = int((time.monotonic() - start) * 1000)
        log_event(name, args, str(result)[:500], duration_ms)
        return result if isinstance(result, str) else json.dumps(result)
    except Exception as e:
        log.exception("Tool call %s failed", name)
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Plan with Claude
# ---------------------------------------------------------------------------

async def _plan_with_claude(
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

    response = _anthropic_client.messages.create(
        model=config.claude_model,
        system=template,
        messages=messages,
        max_tokens=8192,
    )

    result_text = response.content[0].text
    append_planning_history(session_key, "user", context)
    append_planning_history(session_key, "assistant", result_text)

    if response.usage:
        cost = _estimate_cost(response.usage.input_tokens, response.usage.output_tokens)
        log_event("plan_with_claude", {"session_key": session_key}, result_text[:200], 0, session_key, cost)

    if _post_callback:
        asyncio.create_task(_post_callback(result_text))

    return result_text


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
    total_output_tokens = 0
    max_tokens_budget = 50000

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
            total_output_tokens += usage.output_tokens
            cost = _estimate_cost(usage.input_tokens, usage.output_tokens)
            log_event("do_with_claude_iteration", {"iteration": iteration}, "", 0, session_key, cost)

        if response.stop_reason == "end_turn" or not any(
            b.type == "tool_use" for b in response.content
        ):
            text_parts = [b.text for b in response.content if hasattr(b, "text") and b.text]
            result = "\n".join(text_parts)
            if _post_callback:
                asyncio.create_task(_post_callback(result))
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
            if _alert_callback:
                asyncio.create_task(_alert_callback(
                    f"do_with_claude token budget exceeded ({total_output_tokens} tokens)"
                ))
            break

    if _cancel:
        return "Task cancelled by user."

    partial = f"Task reached iteration limit ({max_iterations}). Partial progress made."
    if _alert_callback:
        asyncio.create_task(_alert_callback(partial))
    return partial


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

    save_template(name, new_content)

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


async def _reload_prompts() -> str:
    prompts_clear_cache()

    if _reconnect_callback:
        asyncio.create_task(_reconnect_callback())

    if _alert_callback:
        asyncio.create_task(_alert_callback("Prompts reloaded. Gemini session reconnecting."))

    return json.dumps({"ok": True, "message": "Prompt cache cleared. Session reconnecting."})
