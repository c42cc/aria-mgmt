"""Aria's prompt-management tools — her hands on the Universal Constructor.

These six handlers (list / show / edit / rollback / versions / reload) are how
Aria *wields* the Constructor's library by voice. The pure library, injection,
and version-control logic lives in ``prompts.py``; the improve/eval loop in
``eval.py``. The Aria-side glue these tools need — the Anthropic client (to
perform an edit), the Discord post/alert callbacks, the Gemini reconnect, and
cost/spend accounting — is INJECTED via ``init_prompt_tools`` at boot, so this
module never imports ``bot.py`` or the tool catalog. ``src/tools.py`` keeps the
dispatch catalog (one home) and registers these by name.

Governance: ``edit_prompt`` writes a new version through ``save_template``;
``rollback_prompt`` restores one. The eval layer (``eval.py``) only ADVISES —
user voice edits always win (ARCHITECTURE.md Fundamental 13).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

from ..config import config
from ..db import log_event
from .prompts import (
    clear_cache,
    get_versions,
    list_templates,
    read_raw,
    rollback_template,
    save_template,
)

log = logging.getLogger(__name__)

# --- Injected Aria-side glue (set once by init_prompt_tools at boot) ---------
_anthropic_client: Any = None
_post_callback: Callable[..., Coroutine] | None = None
_alert_callback: Callable[..., Coroutine] | None = None
_reconnect_callback: Callable[..., Coroutine] | None = None
_estimate_cost: Callable[..., float] | None = None
_state_for: Callable[[str], Any] | None = None
_invalidate_model_costs: Callable[[], None] | None = None


def init_prompt_tools(
    *,
    anthropic_client: Any,
    post_callback: Callable[..., Coroutine] | None,
    alert_callback: Callable[..., Coroutine] | None,
    reconnect_callback: Callable[..., Coroutine] | None,
    estimate_cost: Callable[..., float],
    state_for: Callable[[str], Any],
    invalidate_model_costs: Callable[[], None],
) -> None:
    """Inject the Aria-side glue these tools need. Called once from
    ``tools.init_tools`` after the Anthropic client and callbacks exist."""
    global _anthropic_client, _post_callback, _alert_callback, _reconnect_callback
    global _estimate_cost, _state_for, _invalidate_model_costs
    _anthropic_client = anthropic_client
    _post_callback = post_callback
    _alert_callback = alert_callback
    _reconnect_callback = reconnect_callback
    _estimate_cost = estimate_cost
    _state_for = state_for
    _invalidate_model_costs = invalidate_model_costs


async def list_prompts() -> str:
    names = list_templates()
    return json.dumps({"prompts": names})


async def show_prompt(name: str) -> str:
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


async def edit_prompt(name: str, instruction: str, session_key: str = "") -> str:
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


async def rollback_prompt(name: str, version: int) -> str:
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


async def prompt_versions(name: str) -> str:
    versions = get_versions(name)
    if not versions:
        return json.dumps({"name": name, "versions": [], "message": "No version history yet."})
    return json.dumps({"name": name, "versions": versions})


async def reload_prompts() -> str:
    clear_cache()
    if _invalidate_model_costs:
        _invalidate_model_costs()

    if _reconnect_callback:
        await _reconnect_callback()

    if _alert_callback:
        asyncio.create_task(_alert_callback("Prompts reloaded. Gemini session reconnected."))

    return json.dumps({"ok": True, "message": "Prompt cache cleared. Session reconnected."})
