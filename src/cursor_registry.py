"""Single in-process truth for every Cursor agent the system can see.

Owns:

- A workspace_root-keyed map of `CursorAgent` entries.
- A per-session JSONL tailer task that keeps `last_assistant_text`,
  `last_user_text`, and `pending_question` fresh while a Cursor agent is
  active.
- An emit callback (registered by the narrator in `src/bot.py`) that fires
  whenever an agent transitions to an interesting state.

Two ingestion paths converge on the same shape:

- `register_from_hook(hook_type, payload)` is called by
  [src/cursor_external.py](src/cursor_external.py) when the
  `~/.cursor/hooks.json` forwarder POSTs an event about an IDE-opened
  Cursor agent.
- `register_from_sdk(session_id, workspace_root)` is called by
  [src/cursor_bridge.py](src/cursor_bridge.py) when Aria spawns an agent
  via `@cursor/sdk`.

After this module exists the rest of the system stops asking "which path
did this agent come from?" — it asks `cursor_registry.lookup(agent_id)`
and gets back a `CursorAgent` whose fields are populated regardless of
source.

The registry is process-local and in-memory. It does not persist across
restarts; the `~/.cursor/projects/*/agent-transcripts/<sid>/<sid>.jsonl`
files are the durable record. `agents_from_disk()` rehydrates the visible
set when the registry has no prior knowledge of a workspace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

log = logging.getLogger(__name__)


Severity = Literal["high", "low"]
Source = Literal["sdk", "ide", "unknown"]
Status = Literal["running", "waiting", "finished", "errored", "unknown"]
EventKind = Literal["finished", "errored", "question", "started", "progress"]


@dataclass
class SessionInfo:
    """Per-transcript-session state for one workspace."""

    sid: str
    started_at: float
    last_event_at: float
    transcript_path: str | None = None
    last_assistant_text: str = ""
    last_user_text: str = ""
    _tail_offset: int = 0
    _tail_task: asyncio.Task | None = field(default=None, repr=False)
    _tail_mtime: float = 0.0


@dataclass
class CursorAgent:
    """A Cursor agent the registry knows about.

    `agent_id` defaults to `workspace_root`; that is the canonical handle
    Aria's tools take. When more than one session runs in a single
    workspace, callers can pass `<project_label>/<sid_prefix>` to address
    a specific one (see `CursorAgentRegistry.lookup`).
    """

    agent_id: str
    workspace_root: str
    project_label: str
    source: Source = "unknown"
    sessions: dict[str, SessionInfo] = field(default_factory=dict)
    current_sid: str = ""
    status: Status = "unknown"
    last_assistant_text: str = ""
    last_user_text: str = ""
    pending_question: str | None = None
    last_event_at: float = 0.0
    last_event_reason: str = ""
    recent_plan_files: list[str] = field(default_factory=list)
    # Narrator delivery marker: the last `last_event_at` value that has been
    # spoken aloud via Gemini turn_complete=True. Resume / voice-join
    # briefings only surface agents where `last_event_at > last_delivered_at`,
    # so events that were only DM'd (Corbin saw a phone notification but
    # hasn't talked to Aria about them yet) STILL appear on the next voice
    # join. `last_delivered_reason` is the reason text from the last
    # spoken event; for the briefing fallback when nothing has been
    # spoken yet, `last_event_reason` is what we surface instead.
    last_delivered_at: float = 0.0
    last_delivered_reason: str = ""

    def to_public_dict(self) -> dict:
        """JSON-safe projection for tool responses and audits."""
        return {
            "agent_id": self.agent_id,
            "workspace_root": self.workspace_root,
            "project_label": self.project_label,
            "source": self.source,
            "status": self.status,
            "current_sid": self.current_sid,
            "last_assistant_text": self.last_assistant_text,
            "last_user_text": self.last_user_text,
            "pending_question": self.pending_question,
            "last_event_at": self.last_event_at,
            "last_event_reason": self.last_event_reason,
            "last_delivered_at": self.last_delivered_at,
            "recent_plan_files": self.recent_plan_files,
            "sessions": [
                {
                    "sid": s.sid,
                    "started_at": s.started_at,
                    "last_event_at": s.last_event_at,
                    "transcript_path": s.transcript_path,
                }
                for s in sorted(
                    self.sessions.values(),
                    key=lambda s: s.last_event_at,
                    reverse=True,
                )
            ],
        }


@dataclass
class RegistryEvent:
    """What the narrator receives when an agent transitions."""

    kind: EventKind
    agent: CursorAgent
    severity: Severity
    reason: str


EmitCallback = Callable[[RegistryEvent], Awaitable[None]]


# ---------------------------------------------------------------------------
# Helpers shared with src/cursor_external.py
# ---------------------------------------------------------------------------

def _extract_workspace_root(payload: dict) -> str | None:
    """Best-effort: pull a project cwd out of a hook payload.

    Kept independent of `cursor_external._extract_workspace_root` so the
    registry has no import-time dependency on the observer module.
    """
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots:
        first = roots[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            for k in ("path", "uri", "cwd"):
                v = first.get(k)
                if isinstance(v, str):
                    return v.replace("file://", "")
    for k in ("cwd", "workspace_root", "project_path"):
        v = payload.get(k)
        if isinstance(v, str):
            return v
    return None


def _sid_from_transcript_path(path: str | None) -> str:
    """Parse the session id from `~/.cursor/projects/<...>/agent-transcripts/<sid>/<sid>.jsonl`."""
    if not path:
        return ""
    base = os.path.basename(path)
    if base.endswith(".jsonl"):
        return base[: -len(".jsonl")]
    return ""


def _question_in_text(text: str) -> str | None:
    """Heuristic: if the assistant's last block ends in a question, return it.

    Cursor agents that need clarification typically write a short trailing
    paragraph that ends in `?`. We don't try to be clever — false positives
    just mean Aria asks Corbin one extra time.
    """
    if not text:
        return None
    body = text.strip()
    if not body:
        return None
    # Take the last non-empty paragraph.
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    if not paragraphs:
        return None
    last = paragraphs[-1]
    if last.endswith("?"):
        return last[:500]
    return None


def _parse_jsonl_turns(lines: list[str]) -> tuple[str, str, list[str]]:
    """Return (last_assistant_text, last_user_text, plan_file_paths_seen).

    `lines` is a list of raw JSONL lines (no trailing newlines). Plan files
    are surfaced when Cursor's tool_use blocks reference them (best effort —
    Cursor's plan-mode writes are captured separately by
    `cursor_external.list_recent_plans`).
    """
    last_assistant = ""
    last_user = ""
    plans: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = obj.get("role", "")
        message = obj.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text", "")
                if isinstance(t, str):
                    text_parts.append(t)
            elif btype == "tool_use":
                name = (block.get("name") or "").lower()
                if "plan" in name:
                    inp = block.get("input") or {}
                    path = inp.get("path") or inp.get("file_path")
                    if isinstance(path, str):
                        plans.append(path)
        joined = "\n".join(text_parts).strip()
        if joined:
            if role == "assistant":
                last_assistant = joined
            elif role == "user":
                last_user = joined
    return last_assistant, last_user, plans


# ---------------------------------------------------------------------------
# CursorAgentRegistry
# ---------------------------------------------------------------------------

class CursorAgentRegistry:
    """In-memory registry of every Cursor agent we can see."""

    def __init__(
        self,
        *,
        project_aliases: dict[str, str] | None = None,
        tail_interval_sec: float = 2.0,
        tail_idle_grace_sec: float = 60.0,
        truncate_text_chars: int = 1200,
    ) -> None:
        self._agents: dict[str, CursorAgent] = {}
        self._project_aliases: dict[str, str] = dict(project_aliases or {})
        self._emit: EmitCallback | None = None
        self._tail_interval = tail_interval_sec
        self._tail_idle_grace = tail_idle_grace_sec
        self._truncate_chars = truncate_text_chars
        self._lock = asyncio.Lock()
        self._stopping = False

    # -- callbacks ------------------------------------------------------

    def set_emit_callback(self, fn: EmitCallback | None) -> None:
        self._emit = fn

    def set_project_aliases(self, aliases: dict[str, str]) -> None:
        self._project_aliases = dict(aliases)

    # -- lookups --------------------------------------------------------

    def lookup(self, agent_id: str | None) -> CursorAgent | None:
        """Resolve any of the accepted handle shapes to a `CursorAgent`.

        Accepts:
        1. The exact `workspace_root` (canonical agent_id).
        2. A path-like string that normalizes to a known workspace.
        3. A registered alias name (PROJECT_REGISTRY entry).
        4. A `<project_label>/<sid_prefix>` slug for session disambiguation.
        5. A bare `project_label` for the workspace's current session.
        """
        if not agent_id:
            return None
        if agent_id in self._agents:
            return self._agents[agent_id]
        norm = agent_id.rstrip("/")
        if norm in self._agents:
            return self._agents[norm]
        path = self._project_aliases.get(agent_id)
        if path:
            normalized = path.rstrip("/")
            if normalized in self._agents:
                return self._agents[normalized]
        if "/" in agent_id and not agent_id.startswith("/"):
            label, sid_prefix = agent_id.split("/", 1)
            for agent in self._agents.values():
                if agent.project_label != label:
                    continue
                for sid in agent.sessions:
                    if sid.startswith(sid_prefix):
                        return agent
        for agent in self._agents.values():
            if agent.project_label == agent_id:
                return agent
        return None

    def agents(self) -> list[CursorAgent]:
        return list(self._agents.values())

    def __len__(self) -> int:
        return len(self._agents)

    # -- ingestion ------------------------------------------------------

    async def register_from_hook(
        self, hook_type: str, payload: dict
    ) -> CursorAgent | None:
        """Update the registry from a forwarder hook payload.

        Returns the touched `CursorAgent` or `None` if the payload didn't
        identify a workspace. Side effects:

        - Creates the agent record on first sight, source defaults to `ide`.
        - Adds/updates the session keyed by transcript sid.
        - Spawns a JSONL tailer for the session if one isn't running.
        - Emits a `RegistryEvent` to the narrator when the hook represents
          an interesting transition.
        """
        workspace_root = _extract_workspace_root(payload)
        if not workspace_root:
            return None
        workspace_root = workspace_root.rstrip("/")

        agent = self._get_or_create(workspace_root, source="ide")
        sid = _sid_from_transcript_path(payload.get("transcript_path"))
        now = time.time()

        if sid:
            sess = agent.sessions.get(sid)
            if sess is None:
                sess = SessionInfo(
                    sid=sid,
                    started_at=now,
                    last_event_at=now,
                    transcript_path=payload.get("transcript_path"),
                )
                agent.sessions[sid] = sess
            else:
                sess.last_event_at = now
                if not sess.transcript_path:
                    sess.transcript_path = payload.get("transcript_path")
            agent.current_sid = sid

        kind, severity, reason = _classify_hook(hook_type, payload, agent)
        agent.status = _status_from_hook(hook_type, payload, agent.status)
        agent.last_event_at = now

        if sid and sess and sess.transcript_path:
            self._ensure_tailer(agent, sess)

        if kind:
            await self._emit_event(
                RegistryEvent(kind=kind, agent=agent, severity=severity, reason=reason)
            )
        return agent

    async def register_from_sdk(
        self,
        *,
        session_id: str,
        workspace_root: str,
        instruction: str | None = None,
    ) -> CursorAgent:
        """Record an agent the SDK bridge just spawned.

        The SDK is the ground truth here: source=`sdk`, status=`running`.
        If the SDK transcript lands at the same `~/.cursor/projects/...`
        path the IDE uses, the JSONL tailer keeps `last_assistant_text`
        fresh. If it doesn't, the bridge can keep calling
        `record_sdk_event` directly and the registry will still serve
        Aria's `cursor_read` calls.
        """
        workspace_root = workspace_root.rstrip("/")
        agent = self._get_or_create(workspace_root, source="sdk")
        now = time.time()
        if session_id:
            sess = agent.sessions.get(session_id)
            if sess is None:
                sess = SessionInfo(
                    sid=session_id,
                    started_at=now,
                    last_event_at=now,
                    transcript_path=_guess_transcript_path(workspace_root, session_id),
                )
                agent.sessions[session_id] = sess
            agent.current_sid = session_id
            if sess.transcript_path:
                self._ensure_tailer(agent, sess)
        agent.status = "running"
        agent.last_event_at = now
        await self._emit_event(
            RegistryEvent(
                kind="started",
                agent=agent,
                severity="low",
                reason=(
                    f"SDK agent spawned in {agent.project_label}"
                    + (f": {instruction[:120]}" if instruction else "")
                ),
            )
        )
        return agent

    def agent_for_session(self, session_id: str) -> CursorAgent | None:
        """Reverse-lookup by transcript sid. O(N) on workspaces; N is small."""
        if not session_id:
            return None
        for agent in self._agents.values():
            if session_id in agent.sessions:
                return agent
        return None

    async def record_sdk_event(
        self,
        *,
        session_id: str,
        event: str,
        data: dict,
        workspace_root: str | None = None,
    ) -> None:
        """Fold a `@cursor/sdk` stream event into the registry.

        Used as a fallback when the SDK transcript doesn't land on disk in
        the location the tailer expects, or in addition to it. The bridge's
        existing per-session queue keeps draining for `build_with_cursor`
        compatibility; this is a side-channel write-through.

        `workspace_root` is optional — the registry resolves by session_id
        when not provided, which lets the bot's existing event consumer
        forward events with just the sid in hand.
        """
        if workspace_root is None:
            agent = self.agent_for_session(session_id)
        else:
            agent = self._agents.get(workspace_root.rstrip("/"))
        if agent is None:
            return
        now = time.time()
        agent.last_event_at = now
        sess = agent.sessions.get(session_id)
        if sess is None:
            sess = SessionInfo(
                sid=session_id, started_at=now, last_event_at=now
            )
            agent.sessions[session_id] = sess
        sess.last_event_at = now

        kind: EventKind | None = None
        severity: Severity = "low"
        reason = ""

        if event == "assistant":
            message = data.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                texts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                joined = "\n".join(t for t in texts if isinstance(t, str)).strip()
                if joined:
                    agent.last_assistant_text = joined[: self._truncate_chars]
                    sess.last_assistant_text = agent.last_assistant_text
                    q = _question_in_text(joined)
                    agent.pending_question = q
                    if q:
                        kind, severity, reason = (
                            "question",
                            "high",
                            f"{agent.project_label} asked: {q[:200]}",
                        )
                    else:
                        kind, severity, reason = (
                            "progress",
                            "low",
                            f"{agent.project_label} thread {sess.sid[:8]} produced an assistant turn.",
                        )
        elif event == "completion":
            agent.status = "finished"
            kind, severity, reason = (
                "finished",
                "high",
                f"SDK build finished in {agent.project_label}.",
            )
        elif event == "error":
            agent.status = "errored"
            msg = (data.get("message") or "unknown error") if isinstance(data, dict) else "unknown error"
            kind, severity, reason = (
                "errored",
                "high",
                f"SDK build errored in {agent.project_label}: {str(msg)[:200]}",
            )

        if kind:
            await self._emit_event(
                RegistryEvent(kind=kind, agent=agent, severity=severity, reason=reason)
            )

    # -- read paths -----------------------------------------------------

    def transcript_for(self, agent_id: str) -> tuple[CursorAgent | None, list[dict]]:
        """Return (agent, last_assistant + last_user as a small turn list).

        Fast path that does no IO — used by `cursor_read` for the common
        case where the tailer has already populated state. Callers needing
        more than the latest two turns should fall through to the JSONL
        reader in `cursor_external.read_last_n_turns`.
        """
        agent = self.lookup(agent_id)
        if agent is None:
            return None, []
        turns: list[dict] = []
        if agent.last_user_text:
            turns.append({"role": "user", "text": agent.last_user_text})
        if agent.last_assistant_text:
            turns.append({"role": "assistant", "text": agent.last_assistant_text})
        return agent, turns

    # -- shutdown -------------------------------------------------------

    async def stop(self) -> None:
        """Cancel all tailer tasks; called on bot shutdown."""
        self._stopping = True
        tasks: list[asyncio.Task] = []
        for agent in self._agents.values():
            for sess in agent.sessions.values():
                if sess._tail_task and not sess._tail_task.done():
                    sess._tail_task.cancel()
                    tasks.append(sess._tail_task)
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    # -- internals ------------------------------------------------------

    def _get_or_create(self, workspace_root: str, *, source: Source) -> CursorAgent:
        existing = self._agents.get(workspace_root)
        if existing is not None:
            if existing.source == "unknown":
                existing.source = source
            return existing
        label = self._resolve_label(workspace_root)
        agent = CursorAgent(
            agent_id=workspace_root,
            workspace_root=workspace_root,
            project_label=label,
            source=source,
        )
        self._agents[workspace_root] = agent
        return agent

    def _resolve_label(self, workspace_root: str) -> str:
        norm = workspace_root.rstrip("/")
        for name, path in self._project_aliases.items():
            if path.rstrip("/") == norm:
                return name
        return os.path.basename(norm) or norm

    def _ensure_tailer(self, agent: CursorAgent, sess: SessionInfo) -> None:
        if self._stopping:
            return
        if sess._tail_task and not sess._tail_task.done():
            return
        if not sess.transcript_path or not os.path.exists(sess.transcript_path):
            return
        sess._tail_task = asyncio.create_task(
            self._tail_session(agent, sess),
            name=f"cursor_tail:{agent.project_label}:{sess.sid[:8]}",
        )

    async def _tail_session(self, agent: CursorAgent, sess: SessionInfo) -> None:
        """Poll the JSONL file for new turns and update the agent in place."""
        last_event_grace_until = 0.0
        try:
            while not self._stopping:
                try:
                    path = sess.transcript_path
                    if not path or not os.path.exists(path):
                        await asyncio.sleep(self._tail_interval)
                        continue
                    mtime = os.path.getmtime(path)
                    size = os.path.getsize(path)
                    if mtime != sess._tail_mtime or size > sess._tail_offset:
                        sess._tail_mtime = mtime
                        with open(path, "rb") as f:
                            f.seek(sess._tail_offset)
                            new = f.read()
                        sess._tail_offset += len(new)
                        if new:
                            text_lines = new.decode("utf-8", errors="replace").splitlines()
                            la, lu, plans = _parse_jsonl_turns(text_lines)
                            if la:
                                # The session that just produced output IS the
                                # current one. Without this, concurrent threads
                                # in one workspace clobber agent-level fields and
                                # the narration can't say which thread spoke.
                                agent.current_sid = sess.sid
                                agent.last_assistant_text = la[: self._truncate_chars]
                                sess.last_assistant_text = agent.last_assistant_text
                                q = _question_in_text(la)
                                agent.pending_question = q
                                kind: EventKind = "question" if q else "progress"
                                severity: Severity = "high" if q else "low"
                                reason = (
                                    f"{agent.project_label} asked: {q[:200]}"
                                    if q
                                    else f"{agent.project_label} thread {sess.sid[:8]} produced an assistant turn."
                                )
                                await self._emit_event(
                                    RegistryEvent(
                                        kind=kind,
                                        agent=agent,
                                        severity=severity,
                                        reason=reason,
                                    )
                                )
                            if lu:
                                agent.last_user_text = lu[: self._truncate_chars]
                                sess.last_user_text = agent.last_user_text
                            if plans:
                                agent.recent_plan_files = plans[-5:]
                            sess.last_event_at = time.time()
                            agent.last_event_at = sess.last_event_at
                except FileNotFoundError:
                    pass
                except Exception:
                    log.exception(
                        "Tailer for %s/%s raised — backing off one interval",
                        agent.project_label, sess.sid[:8],
                    )

                if agent.status in ("finished", "errored"):
                    if last_event_grace_until == 0:
                        last_event_grace_until = time.time() + self._tail_idle_grace
                    elif time.time() >= last_event_grace_until:
                        return
                else:
                    last_event_grace_until = 0

                await asyncio.sleep(self._tail_interval)
        except asyncio.CancelledError:
            return

    async def _emit_event(self, evt: RegistryEvent) -> None:
        # Stamp the agent with the latest event reason BEFORE firing the
        # callback. This lets briefings on voice-join / pause-resume
        # surface "what just happened in <agent>?" even when the narrator
        # only DM'd it (DM doesn't advance last_delivered_at).
        evt.agent.last_event_reason = evt.reason
        if self._emit is None:
            return
        try:
            await self._emit(evt)
        except Exception:
            log.exception("Cursor registry emit callback raised")


# ---------------------------------------------------------------------------
# Hook classifier (single source of truth for "what kind of event is this?")
# ---------------------------------------------------------------------------

def _classify_hook(
    hook_type: str, payload: dict, agent: CursorAgent
) -> tuple[EventKind | None, Severity, str]:
    """Return (kind, severity, reason). `kind=None` means the hook is noise."""
    status = (payload.get("status") or payload.get("final_status") or "").lower()
    tool_name = (
        payload.get("tool_name")
        or (payload.get("tool_call") or {}).get("name")
        or ""
    )
    label = agent.project_label

    if hook_type == "stop":
        if status == "completed":
            return "finished", "high", f"Cursor task completed in {label}."
        if status == "error":
            return "errored", "high", f"Cursor task errored in {label}."
        if status == "aborted":
            return "finished", "low", f"Cursor task aborted in {label}."
        return "finished", "low", f"Cursor agent loop ended in {label} (status={status or 'unknown'})."

    if hook_type == "subagentStop":
        if status == "error":
            return "errored", "high", f"Cursor subagent errored in {label}."
        return "progress", "low", f"Cursor subagent finished in {label}."

    if hook_type == "postToolUse":
        tn = tool_name.lower().replace(" ", "")
        if "createplan" in tn or "create_plan" in tn:
            return "started", "high", f"Cursor constructed a plan in {label}."
        if tool_name.lower() == "task":
            return "started", "low", f"Cursor dispatched a subagent in {label}."
        return None, "low", ""

    if hook_type == "sessionEnd":
        reason = (payload.get("reason") or "").lower()
        if reason in ("error",):
            return "errored", "high", f"Cursor session ended with error in {label}."
        return None, "low", ""

    return None, "low", ""


def _status_from_hook(hook_type: str, payload: dict, prior: Status) -> Status:
    """Best-effort agent status transition from a hook event."""
    status = (payload.get("status") or payload.get("final_status") or "").lower()
    if hook_type == "stop":
        if status == "completed":
            return "finished"
        if status == "error":
            return "errored"
        if status == "aborted":
            return "finished"
        return prior if prior != "unknown" else "finished"
    if hook_type in ("subagentStop", "postToolUse", "afterAgentResponse"):
        return "running"
    if hook_type == "sessionEnd":
        if (payload.get("reason") or "").lower() == "error":
            return "errored"
        return prior
    return prior


# ---------------------------------------------------------------------------
# SDK transcript path guess
# ---------------------------------------------------------------------------

def _guess_transcript_path(workspace_root: str, session_id: str) -> str | None:
    """Compute the expected JSONL path for a session in a given workspace.

    Mirrors `cursor_external.cursor_project_data_dir` but locally to avoid
    a circular import. If the file doesn't exist (the SDK uses a different
    layout, for instance), the tailer will skip; `record_sdk_event` is the
    fallback ingestion path in that case.
    """
    cursor_dir = os.path.expanduser("~/.cursor")
    base_dir = os.path.join(cursor_dir, "projects")

    def _sanitize(c: str) -> str:
        return c.lstrip("/").replace("/", "-").replace("_", "-")

    candidates = [_sanitize(workspace_root)]
    real = os.path.realpath(workspace_root) if os.path.exists(workspace_root) else workspace_root
    if real != workspace_root:
        candidates.append(_sanitize(real))

    for name in candidates:
        proj_data = os.path.join(base_dir, name)
        sub = os.path.join(proj_data, "agent-transcripts", session_id)
        jsonl = os.path.join(sub, f"{session_id}.jsonl")
        if os.path.exists(jsonl):
            return jsonl
    return None


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

cursor_registry = CursorAgentRegistry()
