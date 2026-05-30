"""Aria's unified Cursor tool surface.

Six tools, one handle. All keyed by `agent_id` (workspace_root by
default; `<label>/<sid_prefix>` slug when multiple sessions share a
workspace). They read from and write to the in-process
`CursorAgentRegistry` in [src/cursor_registry.py](src/cursor_registry.py),
which holds live state for every Cursor agent — IDE-opened or
Aria-spawned — populated by the hook observer and the SDK bridge.

These supersede the older `read_cursor_window`, `send_to_cursor_chat`,
`approve_cursor_plan`, `reject_cursor_plan`, `screenshot_cursor_window`,
`list_cursor_windows`, `list_cursor_plans`, `build_with_cursor`, and
`query_cursor` handlers, which now route through here as thin aliases
(removed in P5).

Routing:

- SDK agents (`agent.source == "sdk"`) talk through the
  `CursorBridge.send_message` / `cancel_session` API. No osascript, no
  focus contests.
- IDE agents (`agent.source == "ide"`) fall back to the existing
  osascript paste-and-send paths in [src/tools.py](src/tools.py). The
  registry feeds them a stable handle so the resolution chain that
  previously failed on ad-hoc workspaces no longer can.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from .cursor_registry import cursor_registry

if TYPE_CHECKING:
    from .cursor_bridge import CursorBridge

log = logging.getLogger(__name__)


# Module-level bridge handle, injected by `init_cursor_tools` at boot.
# Kept out of `cursor_registry.py` so the registry has no IPC concerns.
_bridge: "CursorBridge | None" = None


def init_cursor_tools(bridge: "CursorBridge") -> None:
    """Inject the SDK bridge. Called once from `tools.init_tools`."""
    global _bridge
    _bridge = bridge


def _bridge_required() -> "CursorBridge":
    if _bridge is None:
        raise RuntimeError(
            "cursor_tools not initialized — init_cursor_tools(bridge) must be "
            "called before cursor_send/cursor_spawn can dispatch SDK actions."
        )
    return _bridge


# ---------------------------------------------------------------------------
# cursor_agents — list everything the registry knows about
# ---------------------------------------------------------------------------

async def _cursor_agents() -> str:
    """Return every known Cursor agent and its current state.

    Aria's entry point for "what's running?". Includes IDE windows the
    user opened and SDK agents Aria spawned, on equal footing. Each entry
    carries `agent_id` (the canonical handle for follow-up calls),
    `source`, `status`, `last_assistant_text`, and `pending_question`.
    """
    agents = cursor_registry.agents()
    agents.sort(key=lambda a: a.last_event_at, reverse=True)
    return json.dumps(
        {
            "agents": [a.to_public_dict() for a in agents],
            "count": len(agents),
        }
    )


# ---------------------------------------------------------------------------
# cursor_read — fresh transcript, registry-fast / JSONL-fallback
# ---------------------------------------------------------------------------

async def _cursor_read(agent_id: str, n_turns: int = 5) -> str:
    """Return the most recent transcript turns for an agent.

    Fast path: if the registry's tailer has been running for this
    session, `last_assistant_text` and `last_user_text` are already
    fresh. For more context, the JSONL on disk is read with the
    existing `read_last_n_turns` helper.
    """
    agent = cursor_registry.lookup(agent_id)
    if agent is None:
        return json.dumps(
            {
                "error": (
                    f"Unknown agent_id: {agent_id!r}. "
                    "Call cursor_agents to list current agents."
                ),
            }
        )

    n = max(1, min(int(n_turns) if n_turns else 5, 25))
    turns: list[dict] = []
    sess = agent.sessions.get(agent.current_sid)
    transcript_path = sess.transcript_path if sess else None

    from .cursor_external import list_recent_plans, read_last_n_turns

    if transcript_path and os.path.exists(transcript_path):
        turns = read_last_n_turns(
            agent.workspace_root, n=n, explicit_path=transcript_path
        )
    if not turns:
        if agent.last_user_text:
            turns.append(
                {"role": "user", "text": agent.last_user_text, "has_tool_use": False}
            )
        if agent.last_assistant_text:
            turns.append(
                {
                    "role": "assistant",
                    "text": agent.last_assistant_text,
                    "has_tool_use": False,
                }
            )

    plans = list_recent_plans(max_age_sec=600, limit=5)

    return json.dumps(
        {
            "agent_id": agent.agent_id,
            "workspace_root": agent.workspace_root,
            "project_label": agent.project_label,
            "source": agent.source,
            "status": agent.status,
            "current_sid": agent.current_sid,
            "pending_question": agent.pending_question,
            "turns_returned": len(turns),
            "turns": turns,
            "recent_plans": plans,
        }
    )


# ---------------------------------------------------------------------------
# cursor_send — universal send, routes by source
# ---------------------------------------------------------------------------

_APPROVE_BODY = "Approve and proceed."
_REJECT_BODY = "Stop. Do not proceed with this plan."
_CANCEL_BODY_IDE = "Stop. Cancel this task."


async def _cursor_send(
    agent_id: str,
    message: str = "",
    kind: str = "chat",
    note: str | None = None,
) -> str:
    """Send a message to a Cursor agent. `kind` shapes the body and routing.

    Kinds:
    - `chat` (default): send `message` as-is into the existing agent.
    - `new_agent`: send `message` as a fresh agent task (Cmd+I composer on IDE; new SDK session is `cursor_spawn`, not this).
    - `approve`: send the canonical approval phrase plus optional `note`.
    - `reject`: send the canonical rejection phrase plus optional `note`.
    - `cancel`: cancel via SDK API; for IDE agents, type a stop message.

    Routing:
    - `agent.source == "sdk"` uses the bridge.
    - `agent.source == "ide"` uses the osascript paste-and-send fallback.
    """
    agent = cursor_registry.lookup(agent_id)
    if agent is None:
        return json.dumps(
            {
                "error": (
                    f"Unknown agent_id: {agent_id!r}. "
                    "Call cursor_agents to list current agents."
                ),
            }
        )

    kind_norm = (kind or "chat").lower().strip()
    if kind_norm == "approve":
        body = _APPROVE_BODY + (f" {note}" if note else "")
    elif kind_norm == "reject":
        body = _REJECT_BODY + (f" {note}" if note else "")
    elif kind_norm == "cancel":
        body = (message or _CANCEL_BODY_IDE).strip()
    elif kind_norm in ("chat", "new_agent"):
        body = (message or "").strip()
    else:
        return json.dumps(
            {
                "error": (
                    f"Unknown kind: {kind!r}. "
                    "Use chat, new_agent, approve, reject, or cancel."
                ),
            }
        )

    if not body and kind_norm != "cancel":
        return json.dumps({"error": "Empty message — pass a non-empty `message`."})

    if agent.source == "sdk":
        bridge = _bridge_required()
        if not bridge.alive:
            return json.dumps({"error": "Cursor SDK bridge not alive."})
        sid = agent.current_sid
        if not sid:
            return json.dumps(
                {"error": f"SDK agent {agent.agent_id} has no current session id."}
            )
        try:
            if kind_norm == "cancel":
                resp = await bridge.cancel_session(sid)
                return json.dumps(
                    {
                        "ok": True,
                        "agent_id": agent.agent_id,
                        "route": "sdk_cancel",
                        "result": resp,
                    }
                )
            resp = await bridge.send_message(sid, body)
            return json.dumps(
                {
                    "ok": True,
                    "agent_id": agent.agent_id,
                    "route": "sdk_send",
                    "kind": kind_norm,
                    "result": resp,
                }
            )
        except Exception as e:
            log.exception("cursor_send SDK route failed for %s", agent.agent_id)
            return json.dumps({"error": f"sdk {kind_norm} failed: {e}"})

    # IDE fallback path uses the existing osascript handlers in src/tools.py.
    # Imported lazily so cursor_tools doesn't pull tools at import time.
    from .tools import _send_to_cursor_chat

    project = agent.project_label or agent.workspace_root
    new_agent_flag = kind_norm == "new_agent"
    return await _send_to_cursor_chat(
        project=project, message=body, new_agent=new_agent_flag
    )


# ---------------------------------------------------------------------------
# cursor_spawn — explicit SDK spawn
# ---------------------------------------------------------------------------

async def _cursor_spawn(
    workspace_root: str,
    instruction: str,
    model: str | None = None,
) -> str:
    """Create a fresh `@cursor/sdk` agent in `workspace_root`.

    Returns the canonical `agent_id` the registry assigned. The bridge's
    `create_session` already calls `cursor_registry.register_from_sdk`,
    so the new agent is immediately addressable via `cursor_send` /
    `cursor_read`.
    """
    bridge = _bridge_required()
    if not bridge.alive:
        return json.dumps({"error": "Cursor SDK bridge not alive."})
    workspace_root = workspace_root.rstrip("/")
    if not os.path.isdir(workspace_root):
        return json.dumps(
            {"error": f"workspace_root does not exist: {workspace_root!r}"}
        )
    try:
        sid = await bridge.create_session(workspace_root, instruction, model)
    except Exception as e:
        log.exception("cursor_spawn create_session failed")
        return json.dumps({"error": f"create_session failed: {e}"})
    agent = cursor_registry.agent_for_session(sid)
    return json.dumps(
        {
            "ok": True,
            "agent_id": agent.agent_id if agent else workspace_root,
            "session_id": sid,
            "workspace_root": workspace_root,
        }
    )


# ---------------------------------------------------------------------------
# cursor_screenshot — IDE-only convenience
# ---------------------------------------------------------------------------

async def _cursor_screenshot(agent_id: str, save_path: str | None = None) -> str:
    """Capture a screenshot of an IDE Cursor window.

    No-op for SDK agents (they have no window). For IDE agents, defers to
    the existing `screenshot_cursor_window` handler.
    """
    agent = cursor_registry.lookup(agent_id)
    if agent is None:
        return json.dumps({"error": f"Unknown agent_id: {agent_id!r}"})
    if agent.source == "sdk":
        return json.dumps(
            {
                "ok": False,
                "note": "SDK agents have no IDE window to screenshot.",
                "agent_id": agent.agent_id,
            }
        )
    from .tools import _screenshot_cursor_window

    project = agent.project_label or agent.workspace_root
    return await _screenshot_cursor_window(project=project, save_path=save_path)


# ---------------------------------------------------------------------------
# cursor_status — registry summary + spend
# ---------------------------------------------------------------------------

async def _cursor_status_new() -> str:
    """Compact health summary: registry size, status breakdown, daily spend.

    Distinct from `cursor_agents` which dumps full per-agent state. Use
    `cursor_status` for the at-a-glance "how is the fleet?" view and
    `cursor_agents` to read individual entries.
    """
    from .config import config
    from .db import get_active_cursor_sessions, get_daily_spend

    counts: dict[str, int] = {}
    sources: dict[str, int] = {}
    for agent in cursor_registry.agents():
        counts[agent.status] = counts.get(agent.status, 0) + 1
        sources[agent.source] = sources.get(agent.source, 0) + 1
    return json.dumps(
        {
            "registry_size": len(cursor_registry),
            "status_counts": counts,
            "source_counts": sources,
            "sdk_db_sessions": get_active_cursor_sessions(),
            "daily_spend_usd": get_daily_spend(),
            "daily_cap_usd": config.daily_spend_cap_usd,
        }
    )
