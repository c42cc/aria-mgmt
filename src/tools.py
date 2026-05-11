"""Four tool implementations: plan_with_claude, build_with_cursor, query_cursor, cursor_status."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import anthropic

from .config import config
from .cursor_bridge import CursorBridge
from .db import (
    append_planning_history,
    get_active_cursor_sessions,
    get_planning_history,
    log_event,
    update_cursor_session_event,
    upsert_cursor_session,
)
from .memory import recall
from .prompts import load_template

log = logging.getLogger(__name__)

_anthropic_client: anthropic.Anthropic | None = None
_cursor_bridge: CursorBridge | None = None

PROJECT_REGISTRY: dict[str, str] = {}


def init_tools(cursor_bridge: CursorBridge) -> None:
    """Initialize tool dependencies. Call once at bot startup."""
    global _anthropic_client, _cursor_bridge
    _anthropic_client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    _cursor_bridge = cursor_bridge
    _load_project_registry()


def _load_project_registry() -> None:
    """Parse projects/registry.md into a name → path mapping."""
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
    }
    handler = handlers.get(name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {name}"})

    start = time.monotonic()
    try:
        result = await handler(**args)
        duration_ms = int((time.monotonic() - start) * 1000)
        log_event(name, args, str(result)[:500], duration_ms)
        return result if isinstance(result, str) else json.dumps(result)
    except Exception as e:
        log.exception("Tool call %s failed", name)
        return json.dumps({"error": str(e)})


async def _plan_with_claude(
    context: str,
    session_key: str,
    prompt_template: str = "planning",
) -> str:
    if not _anthropic_client:
        return json.dumps({"error": "Anthropic client not initialized"})

    template = load_template(prompt_template)
    history = get_planning_history(session_key)

    memories = recall(context, limit=3)
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

    return result_text


async def _build_with_cursor(
    project: str,
    instruction: str,
    background: bool = True,
) -> dict[str, Any]:
    if not _cursor_bridge:
        return {"error": "Cursor bridge not initialized"}

    project_path = PROJECT_REGISTRY.get(project)
    if not project_path:
        return {"error": f"Unknown project: {project}. Check projects/registry.md"}

    impl_prompt = load_template("implementation")
    full_instruction = f"{impl_prompt}\n\n---\n\n{instruction}"

    session_id = await _cursor_bridge.create_session(project_path, full_instruction)
    upsert_cursor_session(session_id, project)

    return {"session_id": session_id, "initial_status": "running"}


async def _query_cursor(session_id: str, message: str) -> dict[str, Any]:
    if not _cursor_bridge:
        return {"error": "Cursor bridge not initialized"}
    result = await _cursor_bridge.send_message(session_id, message)
    return {"ok": True, **result}


async def _cursor_status() -> list[dict[str, Any]]:
    return get_active_cursor_sessions()
