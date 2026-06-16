"""Aria's Claude Code driver — the in-process Python Agent SDK bridge.

This is the engine that lets Aria *wield Claude Code* on a target repo (default:
the sibling ``live_visuals_4_CC``). It mirrors the public interface of
``CursorBridge`` — ``create_session`` / ``send_message`` / ``cancel_session`` /
``read_events`` — so the rest of the system (the unified ``cursor_registry``, the
event consumer, the Discord + voice narration) treats a Claude Code thread
exactly like a Cursor one. Unlike the Cursor SDK (JavaScript, hence a Node
sidecar), the Claude Agent SDK is Python, so there is **no sidecar**: the SDK
manages the ``claude`` CLI subprocess itself. Strictly fewer moving parts.

Roles stay separate (Aria's law): Gemini narrates, Claude Opus composes the
instruction, Claude Code builds. This module is the build engine only.

Billing guard — the #1 silent failure. Aria's process loads
``ANTHROPIC_API_KEY`` from ``.env`` (``config.anthropic_api_key`` captured the
value at import, for ``plan_with_claude``'s Anthropic client). The SDK's ``env``
option only *merges on top of* the inherited process environment, so a spawned
``claude`` would inherit that key and silently bill per token, bypassing the Max
subscription. We strip it from this process's environment once at import:
``config`` already holds the value, so reasoning calls are unaffected, and every
``claude`` subprocess now authenticates via the subscription OAuth in
``~/.claude``. ``preflight`` verifies this held.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from .config import config

log = logging.getLogger(__name__)


# ── Billing guard: force the subscription path for spawned `claude` ─────────
# Pop the app's per-token key so it cannot leak into the CLI subprocess env.
# config.anthropic_api_key already holds the value for plan_with_claude.
_STRIPPED_ANTHROPIC_KEY: str | None = os.environ.pop("ANTHROPIC_API_KEY", None)
if _STRIPPED_ANTHROPIC_KEY:
    log.info(
        "claude_code: stripped ANTHROPIC_API_KEY from process env so spawned "
        "`claude` uses the Max subscription (plan_with_claude keeps the captured value)."
    )


# Default repo Aria manages with Claude Code (the migration target).
DEFAULT_CLAUDE_CODE_REPO = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "live_visuals_4_CC",
)

# Tools that are read-only / harmless: auto-approved by the review gate so the
# human is only asked about consequential, world-changing actions.
_SAFE_TOOLS = frozenset({
    "Read", "Glob", "Grep", "LS", "NotebookRead", "TodoWrite", "WebFetch",
    "WebSearch", "Task",
})
# Tools whose proposed input the human should review (and may edit) before it
# fires — the edit-before-submit surface.
_REVIEWABLE_TOOLS = frozenset({
    "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash",
})


# ── Review callback (edit-before-submit), injected by the bot ───────────────
# Signature: async (tool_name, tool_input, session_id, workspace_root) -> dict
#   {"approved": bool, "updated_input": dict | None, "message": str}
ReviewCallback = Callable[..., Awaitable[dict[str, Any]]]
_review_callback: ReviewCallback | None = None


def set_review_callback(fn: ReviewCallback | None) -> None:
    """Inject the human review gate. Called once from bot startup."""
    global _review_callback
    _review_callback = fn


class ClaudeCodeError(RuntimeError):
    """The Claude Code driver hit an unrecoverable condition."""


class _Session:
    """One live Claude Code conversation, keyed by its persistent session UUID.

    Turns fold straight into `cursor_registry` (via `record_claude_code_event`),
    whose emit callback (`bot._narrate_registry_event`) is the single surfacing
    path — the same one that narrates Cursor events to voice / DM / #ucs-alerts.
    No second event queue: one home for "come back to the user".
    """

    def __init__(self, client: ClaudeSDKClient, workspace_root: str) -> None:
        self.client = client
        self.workspace_root = workspace_root
        self.session_id: str = ""
        self.lock = asyncio.Lock()  # serialize one query/response at a time
        self.run_task: asyncio.Task | None = None
        self.closed = False


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------

class ClaudeCodeBridge:
    """In-process driver over the Claude Agent SDK. One `ClaudeSDKClient` per
    session; public surface mirrors `CursorBridge` so consumers are reused."""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._closed = False

    @property
    def alive(self) -> bool:
        # In-process: there is no sidecar to be up or down. The SDK owns the
        # `claude` subprocess per session. Always "alive" once imported.
        return not self._closed

    # -- options -------------------------------------------------------------

    def _build_options(
        self,
        workspace_root: str,
        mode: str,
        model: str | None,
        resume: str | None,
    ) -> ClaudeAgentOptions:
        async def _noop_pre_tool_hook(input_data, tool_use_id, context):
            # Required so `can_use_tool` callbacks fire (keeps the stream open).
            return {}

        kwargs: dict[str, Any] = dict(
            cwd=workspace_root,
            # setting_sources defaults to user+project+local, so the repo's
            # committed .claude/settings.json (Plan Mode + permissions) and
            # .mcp.json (chrome-devtools) load automatically from cwd.
            permission_mode=mode,
            # Cost ceiling per run; the dispatcher's daily cap is the outer bound.
            max_budget_usd=config.claude_code_max_budget_usd,
            model=model or None,
            resume=resume or None,
        )
        # Per-tool review (edit-before-submit) is meaningful ONLY in "default"
        # mode. Plan mode is read-only (the plan IS the review) and acceptEdits
        # auto-accepts edits — wiring can_use_tool there would let the callback
        # OVERRIDE the mode (e.g. allow a write during planning). Gate it.
        if mode == "default":
            kwargs["can_use_tool"] = self._review_tool
            kwargs["hooks"] = {"PreToolUse": [HookMatcher(matcher=None, hooks=[_noop_pre_tool_hook])]}
        return ClaudeAgentOptions(**kwargs)

    # -- the edit-before-submit gate ----------------------------------------

    async def _review_tool(self, tool_name: str, input_data: dict, context: Any):
        """`can_use_tool`: auto-allow safe tools, route consequential ones to the
        human review callback, and honor edits (return `updated_input`)."""
        if tool_name in _SAFE_TOOLS or tool_name not in _REVIEWABLE_TOOLS:
            return PermissionResultAllow()
        if _review_callback is None:
            # No human surface wired — fall back to allow (Plan Mode is the
            # real gate; this path is only hit in default/acceptEdits mode).
            return PermissionResultAllow()
        session = self._session_for_context(context)
        sid = session.session_id if session else ""
        ws = session.workspace_root if session else ""
        try:
            verdict = await _review_callback(
                tool_name=tool_name, tool_input=input_data, session_id=sid, workspace_root=ws,
            )
        except Exception:
            log.exception("claude_code review callback raised — denying for safety")
            return PermissionResultDeny(message="review callback failed")
        if not verdict.get("approved"):
            return PermissionResultDeny(message=verdict.get("message") or "Declined by the user.")
        updated = verdict.get("updated_input")
        if isinstance(updated, dict) and updated:
            return PermissionResultAllow(updated_input=updated)
        return PermissionResultAllow()

    def _session_for_context(self, context: Any) -> _Session | None:
        sid = getattr(context, "session_id", None) or (
            context.get("session_id") if isinstance(context, dict) else None
        )
        if sid and sid in self._sessions:
            return self._sessions[sid]
        # Single in-flight session is the common case.
        running = [s for s in self._sessions.values() if not s.closed]
        return running[0] if len(running) == 1 else None

    # -- lifecycle -----------------------------------------------------------

    async def create_session(
        self,
        workspace_root: str,
        instruction: str,
        mode: str = "plan",
        model: str | None = None,
        resume: str | None = None,
    ) -> str:
        """Spawn a Claude Code session in `workspace_root`, send the first
        instruction, and return its persistent session UUID once known.

        Registers the session in `cursor_registry` (source=claude_code) and
        `claude_sessions` so Aria's read tools and the narrator see it.
        """
        workspace_root = workspace_root.rstrip("/")
        if not os.path.isdir(workspace_root):
            raise ClaudeCodeError(f"workspace_root does not exist: {workspace_root!r}")
        # Defensive re-strip: if anything re-injected the key (e.g. a stray
        # load_dotenv after import), keep the spawned `claude` on the
        # subscription. Absent var -> subscription OAuth in ~/.claude.
        os.environ.pop("ANTHROPIC_API_KEY", None)
        opts = self._build_options(workspace_root, mode, model, resume)
        client = ClaudeSDKClient(options=opts)
        await client.connect()
        session = _Session(client=client, workspace_root=workspace_root)
        ready = asyncio.Event()
        session.run_task = asyncio.create_task(
            self._run(session, instruction, ready, first=True),
            name="claude_code_run",
        )
        try:
            await asyncio.wait_for(ready.wait(), timeout=120)
        except asyncio.TimeoutError:
            await self._safe_disconnect(session)
            raise ClaudeCodeError(
                "Claude Code session did not initialize within 120s "
                "(no session id from the CLI — check `claude` auth / install)."
            )
        return session.session_id

    async def send_message(
        self, session_id: str, message: str, mode: str | None = None
    ) -> dict[str, Any]:
        """Send a follow-up turn to a live session. Optionally switch permission
        mode first (e.g. plan -> acceptEdits to execute an approved plan)."""
        session = self._sessions.get(session_id)
        if session is None or session.closed:
            return {"error": f"No live Claude Code session: {session_id}"}
        if mode:
            try:
                await session.client.set_permission_mode(mode)
            except Exception as e:
                log.warning("set_permission_mode(%s) failed: %s", mode, e)
        # A turn runs in the background so the tool call returns an ack while
        # the response streams to the event consumer.
        asyncio.create_task(
            self._run(session, message, ready=None, first=False),
            name=f"claude_code_send:{session_id[:8]}",
        )
        return {"ok": True, "session_id": session_id, "route": "claude_code_send"}

    async def cancel_session(self, session_id: str) -> dict[str, Any]:
        """Interrupt and tear down a session."""
        session = self._sessions.get(session_id)
        if session is None:
            return {"error": f"No Claude Code session: {session_id}"}
        try:
            await session.client.interrupt()
        except Exception:
            log.debug("interrupt() raised (continuing to disconnect)", exc_info=True)
        await self._safe_disconnect(session)
        return {"ok": True, "session_id": session_id, "route": "claude_code_cancel"}

    # -- synchronous (await-the-turn) variants for harnesses ----------------
    # The bot uses create_session/send_message (fire-and-forget; the registry
    # narrator streams). A sequential driver (e.g. tools/aria_cc_loop_demo.py)
    # wants to await each turn to completion; these do exactly that.

    async def spawn_and_wait(
        self,
        workspace_root: str,
        instruction: str,
        mode: str = "plan",
        model: str | None = None,
        timeout: float = 900,
    ) -> str:
        sid = await self.create_session(workspace_root, instruction, mode=mode, model=model)
        session = self._sessions.get(sid)
        if session and session.run_task:
            try:
                await asyncio.wait_for(asyncio.shield(session.run_task), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        return sid

    async def send_and_wait(
        self, session_id: str, message: str, mode: str | None = None, timeout: float = 900
    ) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None or session.closed:
            return {"error": f"No live Claude Code session: {session_id}"}
        if mode:
            try:
                await session.client.set_permission_mode(mode)
            except Exception as e:
                log.warning("set_permission_mode(%s) failed: %s", mode, e)
        try:
            await asyncio.wait_for(self._run(session, message, None, False), timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "timeout": True, "session_id": session_id}
        return {"ok": True, "session_id": session_id}

    # -- the streaming pump --------------------------------------------------

    async def _run(
        self,
        session: _Session,
        prompt: str,
        ready: asyncio.Event | None,
        first: bool,
    ) -> None:
        """Run one query and drain its response, folding every message."""
        async with session.lock:
            try:
                await session.client.query(prompt)
                async for msg in session.client.receive_response():
                    self._capture_session_id(session, msg, ready)
                    await self._fold(session, msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("claude_code run failed")
                if session.session_id:
                    try:
                        from .cursor_registry import cursor_registry
                        await cursor_registry.record_claude_code_event(
                            session_id=session.session_id, kind="error", error=str(e),
                        )
                    except Exception:
                        log.debug("registry error fold failed", exc_info=True)
                    try:
                        from .db import update_claude_session_event
                        update_claude_session_event(
                            session.session_id, f"error: {e}"[:200], status="error"
                        )
                    except Exception:
                        log.debug("db error stamp failed", exc_info=True)
            finally:
                if ready is not None and not ready.is_set():
                    # Unblock create_session even if no id ever arrived; the
                    # timeout path in create_session will surface the failure.
                    ready.set()

    def _capture_session_id(
        self, session: _Session, msg: Any, ready: asyncio.Event | None
    ) -> None:
        if session.session_id:
            return
        sid = getattr(msg, "session_id", None)
        if not sid and isinstance(msg, SystemMessage):
            sid = (msg.data or {}).get("session_id")
        if not sid:
            return
        session.session_id = sid
        self._sessions[sid] = session
        try:
            from .cursor_registry import cursor_registry
            asyncio.create_task(
                cursor_registry.register_from_claude_code(
                    session_id=sid, workspace_root=session.workspace_root,
                )
            )
        except Exception:
            log.exception("register_from_claude_code failed for %s", sid)
        try:
            from .db import upsert_claude_session
            upsert_claude_session(
                sid,
                os.path.basename(session.workspace_root) or session.workspace_root,
                workspace_root=session.workspace_root,
            )
        except Exception:
            log.debug("upsert_claude_session failed", exc_info=True)
        if ready is not None and not ready.is_set():
            ready.set()

    async def _fold(self, session: _Session, msg: Any) -> None:
        """Translate one SDK message into a normalized event + registry update."""
        from .cursor_registry import cursor_registry

        if isinstance(msg, AssistantMessage):
            text = "\n".join(
                b.text for b in msg.content if isinstance(b, TextBlock)
            ).strip()
            if text:
                await cursor_registry.record_claude_code_event(
                    session_id=session.session_id, kind="assistant", text=text,
                )
            return

        if isinstance(msg, ResultMessage):
            # total_cost_usd is the subscription's *notional* cost for the turn.
            # It is recorded per-session (claude_sessions.cost_usd) for
            # observability but deliberately NOT added to the real-API daily
            # spend cap — Claude Code is bounded per run by max_budget_usd.
            cost = float(getattr(msg, "total_cost_usd", 0.0) or 0.0)
            subtype = getattr(msg, "subtype", "") or ""
            is_error = bool(getattr(msg, "is_error", False))
            if is_error or subtype.startswith("error"):
                detail = getattr(msg, "result", None) or subtype or "error"
                await cursor_registry.record_claude_code_event(
                    session_id=session.session_id, kind="error", error=str(detail),
                )
                try:
                    from .db import update_claude_session_event
                    update_claude_session_event(
                        session.session_id, f"{subtype}", status="error", add_cost_usd=cost,
                    )
                except Exception:
                    log.debug("db error stamp failed", exc_info=True)
            else:
                await cursor_registry.record_claude_code_event(
                    session_id=session.session_id, kind="completion",
                )
                try:
                    from .db import update_claude_session_event
                    update_claude_session_event(
                        session.session_id, "turn complete", status="waiting", add_cost_usd=cost,
                    )
                except Exception:
                    log.debug("db completion stamp failed", exc_info=True)
            return

    # -- teardown ------------------------------------------------------------

    async def _safe_disconnect(self, session: _Session) -> None:
        session.closed = True
        try:
            await session.client.disconnect()
        except Exception:
            log.debug("claude client disconnect raised", exc_info=True)
        if session.run_task and not session.run_task.done():
            session.run_task.cancel()
        if session.session_id:
            self._sessions.pop(session.session_id, None)

    async def stop(self) -> None:
        """Tear down every live session. Called on bot shutdown."""
        self._closed = True
        for session in list(self._sessions.values()):
            await self._safe_disconnect(session)


# Module singleton (mirrors `cursor_registry`); the bot also constructs one in
# bot.py and passes it to init_tools, but a module handle keeps the tools simple.
claude_code_bridge = ClaudeCodeBridge()


def _bridge() -> ClaudeCodeBridge:
    return claude_code_bridge


# ---------------------------------------------------------------------------
# Workspace resolution (shared shape with cursor_tools)
# ---------------------------------------------------------------------------

def _resolve_workspace_root(project: str) -> str | None:
    """Map a project name/alias/path to an absolute workspace_root, defaulting
    to the Claude Code repo. Mirrors cursor_tools._resolve_workspace_root with a
    `live_visuals_4_CC` default so a bare call lands on the managed repo."""
    p = (project or "").strip()
    if not p:
        return DEFAULT_CLAUDE_CODE_REPO if os.path.isdir(DEFAULT_CLAUDE_CODE_REPO) else None
    try:
        from .cursor_tools import _resolve_workspace_root as _shared
        resolved = _shared(p)
        if resolved:
            return resolved
    except Exception:
        pass
    if os.path.isabs(p) and os.path.isdir(p):
        return p.rstrip("/")
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

async def _claude_code_spawn(
    workspace_root: str = "",
    instruction: str = "",
    mode: str = "plan",
) -> str:
    """Start a NEW Claude Code thread in `workspace_root` and hand it an
    instruction. Defaults to the managed `live_visuals_4_CC` repo and Plan Mode
    (so it proposes a plan for review before any edit). Returns the agent_id +
    session_id; follow up with `claude_code_read` / `claude_code_send`."""
    ws = _resolve_workspace_root(workspace_root)
    if not ws:
        return json.dumps({
            "error": f"Could not resolve workspace {workspace_root!r}. Pass an "
                     "absolute path or a known project name.",
        })
    if not instruction.strip():
        return json.dumps({"error": "instruction is required."})
    mode = mode if mode in ("plan", "default", "acceptEdits", "bypassPermissions") else "plan"
    try:
        sid = await _bridge().create_session(ws, instruction, mode=mode)
    except Exception as e:
        log.exception("claude_code_spawn failed")
        return json.dumps({"error": f"spawn failed: {e}"})
    try:
        from .db import set_ground
        set_ground(
            "active_project",
            label=os.path.basename(ws) or ws,
            path=ws,
            detail=f"claude code session {sid[:8]} (mode={mode})",
            source="claude_code_spawn",
        )
    except Exception:
        log.debug("ground write failed", exc_info=True)
    return json.dumps({
        "ok": True,
        "agent_id": ws,
        "session_id": sid,
        "workspace_root": ws,
        "mode": mode,
        "note": "Plan Mode: it will propose a plan; review/edit, then claude_code_send "
                "'proceed' (mode=acceptEdits) to execute." if mode == "plan" else "",
    })


async def _claude_code_send(
    agent_id: str = "",
    message: str = "",
    kind: str = "chat",
    mode: str = "",
) -> str:
    """Send a follow-up to a live Claude Code thread, or approve/reject/cancel it.

    kinds: chat (send `message`), approve (proceed with the plan — switches to
    acceptEdits), reject (stop), cancel (tear down). Resolve the agent_id from
    `claude_code_read` / `cursor_agents`."""
    from .cursor_registry import cursor_registry

    agent = cursor_registry.lookup(agent_id)
    if agent is None or agent.source != "claude_code" or not agent.current_sid:
        return json.dumps({
            "error": f"No live Claude Code thread for agent_id {agent_id!r}. "
                     "Call cursor_agents to list threads.",
        })
    sid = agent.current_sid
    kind_norm = (kind or "chat").lower().strip()
    bridge = _bridge()
    if kind_norm == "cancel":
        return json.dumps(await bridge.cancel_session(sid))
    if kind_norm == "approve":
        body = (message or "Proceed with the approved plan.").strip()
        send_mode = mode or "acceptEdits"
    elif kind_norm == "reject":
        body = (message or "Stop. Do not proceed with this plan.").strip()
        send_mode = mode or "plan"
    else:
        body = (message or "").strip()
        send_mode = mode or None
    if not body:
        return json.dumps({"error": "Empty message."})
    return json.dumps(await bridge.send_message(sid, body, mode=send_mode))


async def _claude_code_read(agent_id: str = "", n_turns: int = 5) -> str:
    """Read the latest turns + status of a Claude Code thread.

    Defaults to the managed repo's current thread. Returns live registry state
    (kept fresh by the driver's stream) plus any pending question."""
    from .cursor_registry import cursor_registry

    raw = (agent_id or "").strip() or DEFAULT_CLAUDE_CODE_REPO
    agent = cursor_registry.lookup(raw)
    if agent is None:
        return json.dumps({
            "error": f"Unknown Claude Code thread {agent_id!r}. Call cursor_agents.",
        })
    turns: list[dict] = []
    if agent.last_user_text:
        turns.append({"role": "user", "text": agent.last_user_text})
    if agent.last_assistant_text:
        turns.append({"role": "assistant", "text": agent.last_assistant_text})
    return json.dumps({
        "agent_id": agent.agent_id,
        "workspace_root": agent.workspace_root,
        "project_label": agent.project_label,
        "source": agent.source,
        "status": agent.status,
        "current_sid": agent.current_sid,
        "pending_question": agent.pending_question,
        "turns": turns[-max(1, min(int(n_turns or 5), 25)):],
    })


async def _claude_code_threads(project: str = "") -> str:
    """List Claude Code threads Aria is driving (live registry + active DB rows)."""
    from .cursor_registry import cursor_registry
    from .db import get_active_claude_sessions

    ws = _resolve_workspace_root(project)
    rows = []
    for agent in cursor_registry.agents():
        if agent.source != "claude_code":
            continue
        if ws and agent.workspace_root.rstrip("/") != ws.rstrip("/"):
            continue
        rows.append({
            "agent_id": agent.agent_id,
            "project_label": agent.project_label,
            "status": agent.status,
            "current_sid": agent.current_sid[:8],
            "pending_question": agent.pending_question,
            "last_assistant_text": (agent.last_assistant_text or "")[:400],
        })
    return json.dumps({
        "project": os.path.basename(ws) if ws else None,
        "count": len(rows),
        "threads": rows,
        "active_db_sessions": get_active_claude_sessions(),
    })
