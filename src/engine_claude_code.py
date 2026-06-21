"""The engine — Claude Code via the Agent SDK (a managed `claude` subprocess).

One body does the work. The SDK runs the `claude` binary as a subprocess and
streams events back (review 3.3 — there is no in-process Claude Code). This
module only RUNS an instruction and reports what happened; the dispatcher
enforces "done" against ground truth (git diff + tests), never trusting the
engine's narration.

Billing is explicit (review 3.1): `subscription` strips ANTHROPIC_API_KEY from
the subprocess env so it uses the Claude subscription; `api` sets the key to pay
PAYG. A wrong/absent auth fails loudly with the one-line fix — never a silent
fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from .config import config

log = logging.getLogger(__name__)


@dataclass
class EngineResult:
    ok: bool
    session_id: str
    text: str
    cost_usd: float
    error: str | None = None


def _prepare_billing() -> None:
    mode = config.claude_code_billing
    if mode == "subscription":
        os.environ.pop("ANTHROPIC_API_KEY", None)
    elif mode == "api":
        if not config.anthropic_api_key:
            raise RuntimeError(
                "ARIA_CLAUDE_CODE_BILLING=api but ANTHROPIC_API_KEY is empty — set it in .env or use billing=subscription."
            )
        os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    else:
        raise RuntimeError(f"unknown ARIA_CLAUDE_CODE_BILLING={mode!r} (expected 'subscription' or 'api')")


async def _run_async(workspace_root: str, instruction: str) -> EngineResult:
    opts = ClaudeAgentOptions(
        cwd=workspace_root,
        permission_mode=config.claude_code_permission_mode,
        max_budget_usd=config.claude_code_max_budget_usd,
    )
    texts: list[str] = []
    sid = ""
    cost = 0.0
    err: str | None = None
    client = ClaudeSDKClient(options=opts)
    await client.connect()
    try:
        await client.query(instruction)
        async for msg in client.receive_response():
            if not sid:
                sid = getattr(msg, "session_id", "") or (
                    (msg.data or {}).get("session_id", "") if isinstance(msg, SystemMessage) else ""
                )
            if isinstance(msg, AssistantMessage):
                chunk = "\n".join(b.text for b in msg.content if isinstance(b, TextBlock)).strip()
                if chunk:
                    texts.append(chunk)
            elif isinstance(msg, ResultMessage):
                cost = float(getattr(msg, "total_cost_usd", 0.0) or 0.0)
                if getattr(msg, "is_error", False) or str(getattr(msg, "subtype", "")).startswith("error"):
                    err = str(getattr(msg, "result", None) or getattr(msg, "subtype", "") or "engine error")
    finally:
        try:
            await client.disconnect()
        except Exception:
            log.debug("engine disconnect raised", exc_info=True)
    return EngineResult(
        ok=(err is None),
        session_id=sid,
        text="\n".join(texts).strip(),
        cost_usd=cost,
        error=err,
    )


def run(workspace_root: str, instruction: str) -> EngineResult:
    """Run one instruction to completion in `workspace_root`. Synchronous wrapper
    over the async SDK (one dispatch at a time in Phase 0)."""
    if not os.path.isdir(workspace_root):
        raise RuntimeError(f"engine workspace does not exist: {workspace_root!r}")
    _prepare_billing()
    return asyncio.run(asyncio.wait_for(_run_async(workspace_root, instruction), timeout=config.claude_code_timeout_sec))
