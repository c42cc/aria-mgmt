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
Source = Literal["sdk", "ide", "claude_code", "unknown"]
Status = Literal["running", "waiting", "finished", "errored", "unknown"]
EventKind = Literal[
    "finished", "errored", "question", "started", "progress", "cancelled", "stalled"
]

# A thread whose status is "running" but whose last hook landed longer ago than
# this is no longer treated as *live*-running (the agent likely finished and we
# missed/never got a stop hook, or it's stuck). We never fabricate a downgrade —
# the raw status + the age are both surfaced — but "is it processing NOW?" keys
# off recency so a stale "running" can't masquerade as active work.
RUNNING_RECENCY_SEC = 180.0


@dataclass
class SessionInfo:
    """Per-transcript-session state for one workspace."""

    sid: str
    started_at: float
    last_event_at: float
    transcript_path: str | None = None
    last_assistant_text: str = ""
    last_user_text: str = ""
    # Per-THREAD live status, driven by THIS session's own hooks (not the
    # workspace's). The granularity the user actually works in.
    status: Status = "unknown"
    last_event_reason: str = ""
    pending_question: str | None = None
    recent_plan_files: list[str] = field(default_factory=list)
    # The last stop-hook status seen for this thread ("completed"/"error"/
    # "aborted"/"running"). Only disambiguates error vs cancel at settle; the
    # transcript itself is the source of truth for "did it hand back".
    last_hook_status: str = "running"
    _tail_offset: int = 0
    _tail_task: asyncio.Task | None = field(default=None, repr=False)
    _tail_mtime: float = 0.0
    # Lifecycle-watcher state: when bytes last arrived (the settle clock), and
    # the transcript offset at which we last emitted a terminal buzz (mirrors the
    # durable db watermark so the hot path doesn't hit sqlite every poll).
    _last_byte_at: float = 0.0
    _delivered_offset: int = -1


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
        """JSON-safe projection for tool responses and audits.

        Status is recomputed at read time so recency is honest: a thread that
        was "running" but has been quiet beyond the window is reported with its
        raw status PLUS its age and `live=false`, never masquerading as active.
        """
        now = time.time()
        return {
            "agent_id": self.agent_id,
            "workspace_root": self.workspace_root,
            "project_label": self.project_label,
            "source": self.source,
            "status": _aggregate_agent_status(self, now),
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
                    "status": s.status,
                    "age_sec": round(now - s.last_event_at, 1),
                    "live": _session_live(s, now),
                    "last_event_reason": s.last_event_reason,
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


# Tools whose use means "I am asking the user to make a decision RIGHT NOW."
# This is the ONLY honest "stopped to wait for you" signal — an agent that calls
# one of these has halted for the user, which is exactly (and only) when Aria
# pings. A question is asked by CALLING this tool, never inferred from prose: a
# trailing-'?' heuristic was deleted (2026-06-19) because it fabricated "it's
# asking you" out of any turn that merely ended in a question mark — including a
# dev assistant's own writing — buzzing the user about questions that never
# existed.
_ASK_TOOL_NAMES = {"askquestion", "askfollowupquestion", "askfollowup", "askuser"}


def _normalize_tool_name(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _extract_ask_question(content: object) -> str | None:
    """Return the prompt of an 'ask the user' tool call in an assistant turn.

    Reads the `tool_use` block directly. This is the single source of a
    `question` event: the moment the agent emits an explicit ask we have the
    question text and surface it as a high-severity decision. There is no prose
    fallback — inferring a question from a trailing '?' fabricated decisions the
    agent never asked.
    """
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if _normalize_tool_name(block.get("name", "")) not in _ASK_TOOL_NAMES:
            continue
        inp = block.get("input")
        if not isinstance(inp, dict):
            return "needs a decision (open Cursor)"
        # AskQuestion shape: {"questions": [{"prompt": .., "options": [..]}], ..}
        questions = inp.get("questions")
        if isinstance(questions, list) and questions:
            prompts: list[str] = []
            for q in questions:
                if isinstance(q, dict):
                    pr = q.get("prompt") or q.get("question") or q.get("title")
                    if isinstance(pr, str) and pr.strip():
                        prompts.append(pr.strip())
            if prompts:
                extra = f" (+{len(prompts) - 1} more)" if len(prompts) > 1 else ""
                return prompts[0][:400] + extra
        # Simple shapes: {"question"/"prompt"/"title"/"message": ".."}
        for key in ("question", "prompt", "title", "message"):
            v = inp.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()[:400]
        return "needs a decision (open Cursor)"
    return None


def _parse_jsonl_turns(
    lines: list[str],
) -> tuple[str, str, list[str], str | None]:
    """Return (last_assistant_text, last_user_text, plan_file_paths, ask_question).

    `lines` is a list of raw JSONL lines (no trailing newlines). Plan files
    are surfaced when Cursor's tool_use blocks reference them. `ask_question`
    is the prompt of the most recent 'ask the user' tool call (e.g.
    `AskQuestion`) seen in an assistant turn, or None — the reliable decision
    signal that the prose heuristic misses.
    """
    last_assistant = ""
    last_user = ""
    plans: list[str] = []
    last_ask: str | None = None
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
        if role == "assistant":
            aq = _extract_ask_question(content)
            if aq:
                last_ask = aq
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
    return last_assistant, last_user, plans, last_ask


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

    def live_status_for_sid(self, sid: str) -> dict | None:
        """Live per-thread status from the hook stream, matched by transcript
        sid (full or a prefix). Returns {status, live, age_sec, last_event_at,
        reason} or None if the registry has never seen this thread. This is the
        ground-truth "is it processing now?" that overlays the distilled roster.
        """
        if not sid:
            return None
        now = time.time()
        for agent in self._agents.values():
            sess = agent.sessions.get(sid)
            if sess is None:
                for s_sid, s in agent.sessions.items():
                    if s_sid.startswith(sid) or sid.startswith(s_sid):
                        sess = s
                        break
            if sess is not None:
                return {
                    "status": sess.status,
                    "live": _session_live(sess, now),
                    "age_sec": round(now - sess.last_event_at, 1),
                    "last_event_at": sess.last_event_at,
                    "reason": sess.last_event_reason,
                }
        return None

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

        # The hook is now a WAKE + a status HINT, never a buzz. Emission is owned
        # by the lifecycle watcher (the per-session tailer), which derives the
        # surfaced state from the TRANSCRIPT when the thread SETTLES — so a
        # per-turn stop hook no longer fires a per-turn "completed". We record the
        # stop status only so the watcher can tell an error/cancel apart from a
        # normal hand-back; ensure the tailer is running and let it own the rest.
        if sid:
            tracked = agent.sessions.get(sid)
            if tracked is not None:
                tracked.last_hook_status = _hook_terminal_status(hook_type, payload)
                tracked.status = _status_from_hook(hook_type, payload, tracked.status)
        agent.status = _aggregate_agent_status(agent, now)
        agent.last_event_at = now

        if sid and sess and sess.transcript_path:
            self._ensure_tailer(agent, sess)
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

    async def register_from_claude_code(
        self,
        *,
        session_id: str,
        workspace_root: str,
        instruction: str | None = None,
    ) -> CursorAgent:
        """Record an agent Aria spawned via the Claude Agent SDK (Claude Code).

        Source=`claude_code`, status=`running`. Unlike the Cursor SDK path,
        the driver in `src/claude_code.py` streams turns directly and folds
        them via `record_claude_code_event`, so no JSONL tailer is started
        here (Claude Code writes its transcript under `~/.claude/...`, a
        different layout than the `~/.cursor/...` tailer expects).
        """
        workspace_root = workspace_root.rstrip("/")
        agent = self._get_or_create(workspace_root, source="claude_code")
        # A workspace previously seen as a Cursor agent can also host a Claude
        # Code session; the live source follows the most recent spawn.
        agent.source = "claude_code"
        now = time.time()
        if session_id:
            sess = agent.sessions.get(session_id)
            if sess is None:
                sess = SessionInfo(
                    sid=session_id,
                    started_at=now,
                    last_event_at=now,
                    transcript_path=_guess_claude_transcript_path(
                        workspace_root, session_id
                    ),
                )
                agent.sessions[session_id] = sess
            agent.current_sid = session_id
        agent.status = "running"
        agent.last_event_at = now
        await self._emit_event(
            RegistryEvent(
                kind="started",
                agent=agent,
                severity="low",
                reason=(
                    f"Claude Code session started in {agent.project_label}"
                    + (f": {instruction[:120]}" if instruction else "")
                ),
            )
        )
        return agent

    async def record_claude_code_event(
        self,
        *,
        session_id: str,
        kind: str,
        text: str = "",
        error: str = "",
    ) -> None:
        """Fold one Claude Code stream turn into the registry.

        `kind` is the already-classified turn from the driver:
        `assistant` (text is the assistant turn), `completion` (the run ended
        cleanly), or `error` (error is the failure detail). The driver does
        the SDK-message parsing; the registry owns the agent-state transition
        and the narrator emit — the same split as `record_sdk_event` for the
        Cursor SDK.
        """
        agent = self.agent_for_session(session_id)
        if agent is None:
            return
        now = time.time()
        agent.last_event_at = now
        sess = agent.sessions.get(session_id)
        if sess is None:
            sess = SessionInfo(sid=session_id, started_at=now, last_event_at=now)
            agent.sessions[session_id] = sess
        sess.last_event_at = now
        agent.current_sid = session_id

        evt_kind: EventKind | None = None
        severity: Severity = "low"
        reason = ""
        if kind == "assistant" and text:
            agent.last_assistant_text = text[: self._truncate_chars]
            sess.last_assistant_text = agent.last_assistant_text
            # This path receives the assistant text only — no structured
            # tool_use blocks — so there is no explicit AskQuestion to detect
            # here. A Claude Code turn is therefore always progress, never a
            # manufactured question. (The trailing-'?' prose heuristic that used
            # to run here was deleted: it fabricated "it's asking you".)
            agent.pending_question = None
            evt_kind, severity, reason = (
                "progress", "low",
                f"{agent.project_label} Claude Code thread {sess.sid[:8]} produced a turn.",
            )
        elif kind == "completion":
            agent.status = "finished"
            evt_kind, severity, reason = (
                "finished", "high", f"Claude Code run finished in {agent.project_label}.",
            )
        elif kind == "error":
            agent.status = "errored"
            evt_kind, severity, reason = (
                "errored", "high",
                f"Claude Code run errored in {agent.project_label}: {str(error)[:200]}",
            )
        if evt_kind:
            await self._emit_event(
                RegistryEvent(kind=evt_kind, agent=agent, severity=severity, reason=reason)
            )

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
                ask_q = _extract_ask_question(content)
                if joined or ask_q:
                    if joined:
                        agent.last_assistant_text = joined[: self._truncate_chars]
                        sess.last_assistant_text = agent.last_assistant_text
                    # A question exists ONLY when the agent made an explicit
                    # ask-the-user tool call — never inferred from prose.
                    q = ask_q
                    agent.pending_question = q
                    if q:
                        kind, severity, reason = (
                            "question",
                            "high",
                            f"{agent.project_label} is asking: {q[:200]}",
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

    async def ensure_thread_from_disk(
        self, workspace_root: str, sid: str, transcript_path: str
    ) -> None:
        """Attach a tailer to a thread discovered on disk (its first hook never
        arrived, or hooks are dark). No emit here — the tailer owns surfaced state
        from now on. Idempotent: a running tailer is left untouched."""
        if not workspace_root or not sid or not transcript_path:
            return
        workspace_root = workspace_root.rstrip("/")
        agent = self._get_or_create(workspace_root, source="ide")
        now = time.time()
        sess = agent.sessions.get(sid)
        if sess is None:
            sess = SessionInfo(
                sid=sid, started_at=now, last_event_at=now,
                transcript_path=transcript_path,
            )
            agent.sessions[sid] = sess
        elif not sess.transcript_path:
            sess.transcript_path = transcript_path
        self._ensure_tailer(agent, sess)

    async def discover_threads_from_disk(self, *, window_hours: float = 0.5) -> int:
        """Attach a tailer to every recently-active thread on disk across all
        known project roots. Hook-independent discovery — the watcher's guard
        against a silent blind spot when a thread's first hook never lands.
        Returns the number of threads ensured. Idempotent."""
        from .cursor_external import list_recent_threads
        roots: set[str] = {p for p in self._project_aliases.values() if p}
        roots |= {a.workspace_root for a in self._agents.values() if a.workspace_root}
        n = 0
        for root in roots:
            try:
                threads = list_recent_threads(root, window_hours=window_hours, limit=30)
            except Exception:
                log.exception("discovery: list_recent_threads failed for %s", root)
                continue
            for t in threads:
                await self.ensure_thread_from_disk(
                    root.rstrip("/"), t.get("sid", ""), t.get("transcript_path", "")
                )
                n += 1
        return n

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
        """Own ONE thread's surfaced state. Read its transcript incrementally and,
        when it SETTLES (quiet >= settle_seconds), emit exactly one terminal buzz
        derived from the transcript tail — never a per-turn hook. Once attached,
        this polls the file directly, so it keeps working even if every later hook
        is dark (the discovery sweep attaches one for a thread whose first hook
        never arrived).
        """
        from .config import config
        from . import db

        # Floor guards against a zero/negative misconfig; the real value is the
        # config default (12s). hung is always >= settle.
        settle = max(0.05, config.cursor_settle_seconds)
        hung = max(settle, config.cursor_hung_minutes * 60.0)

        # Seed the durable watermark on first attach: everything already in the
        # transcript is HISTORY (it settled before we were watching), so seed
        # delivered_offset to the current size — only growth AFTER attach can fire
        # a terminal buzz. This is the one anti-replay guard (restart-safe and
        # backlog-safe); it replaces the in-memory 120s dedup cooldown entirely.
        if sess._delivered_offset < 0:
            durable_off, _ = db.get_cursor_delivered(sess.sid)
            try:
                cur_size = (
                    os.path.getsize(sess.transcript_path)
                    if sess.transcript_path and os.path.exists(sess.transcript_path)
                    else 0
                )
            except OSError:
                cur_size = 0
            sess._delivered_offset = max(durable_off, cur_size, sess._tail_offset)
            sess._last_byte_at = time.time()

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
                            la, lu, plans, ask_q = _parse_jsonl_turns(text_lines)
                            if la or ask_q:
                                # The session that just produced output IS the
                                # current one. Without this, concurrent threads in
                                # one workspace clobber agent-level fields and the
                                # narration can't say which thread spoke.
                                agent.current_sid = sess.sid
                                if la:
                                    agent.last_assistant_text = la[: self._truncate_chars]
                                    sess.last_assistant_text = agent.last_assistant_text
                                # An explicit ask-the-user tool call is the ONLY
                                # decision signal; never inferred from prose.
                                sess.pending_question = ask_q
                                agent.pending_question = ask_q
                            if lu:
                                agent.last_user_text = lu[: self._truncate_chars]
                                sess.last_user_text = agent.last_user_text
                                # The user spoke -> any pending question is answered.
                                sess.pending_question = None
                                agent.pending_question = None
                            if plans:
                                sess.recent_plan_files = plans[-5:]
                                agent.recent_plan_files = plans[-5:]
                            sess.last_event_at = time.time()
                            agent.last_event_at = sess.last_event_at
                            # New bytes => not a hand-back yet; re-arm the settle.
                            sess._last_byte_at = sess.last_event_at
                except FileNotFoundError:
                    pass
                except Exception:
                    log.exception(
                        "Tailer for %s/%s raised — backing off one interval",
                        agent.project_label, sess.sid[:8],
                    )

                # SETTLE: quiet long enough to be a real hand-back (or a hang).
                # Classify ONCE per halt from the transcript; the durable
                # watermark makes it idempotent across polls AND restarts.
                quiet_for = time.time() - sess._last_byte_at
                if sess._tail_offset > sess._delivered_offset and quiet_for >= settle:
                    terminal = self._classify_terminal(agent, sess, quiet_for, hung)
                    if terminal is not None:
                        kind, severity, reason = terminal
                        sess._delivered_offset = sess._tail_offset
                        sess.status = _status_for_kind(kind, sess.status)
                        agent.status = _aggregate_agent_status(agent, time.time())
                        try:
                            db.set_cursor_delivered(
                                sess.sid, sess._tail_offset, kind,
                                project=agent.project_label, summary=reason[:200],
                            )
                        except Exception:
                            log.exception("watermark persist failed for %s", sess.sid[:8])
                        await self._emit_event(
                            RegistryEvent(kind=kind, agent=agent, severity=severity, reason=reason)
                        )

                # Reap a fully-delivered, long-quiet tailer to bound live tasks;
                # the discovery sweep re-attaches a fresh one if it resumes (new
                # growth -> a new halt past this watermark -> a new buzz).
                if sess._tail_offset <= sess._delivered_offset and quiet_for >= hung:
                    return

                await asyncio.sleep(self._tail_interval)
        except asyncio.CancelledError:
            return

    def _classify_terminal(
        self, agent: CursorAgent, sess: SessionInfo, quiet_for: float, hung: float
    ) -> tuple[EventKind, Severity, str] | None:
        """The single classifier: what state did this thread hand back in?

        Pure function of the transcript tail + the one thing the transcript can't
        show (error vs user-cancel, from the stop hook). Returns the terminal
        event, or None to keep waiting. Every other moment — mid-task turns,
        subagent finishes, progress — is silent by construction (no emit path).
        """
        headline = f"{agent.project_label} thread {sess.sid[:8]}"
        # A decision the agent explicitly stopped on -> needs you NOW.
        if sess.pending_question:
            return ("question", "high", f"{headline} is asking: {sess.pending_question[:240]}")
        if sess.recent_plan_files:
            return ("question", "high", f"{headline} built a plan awaiting your approval.")
        # The stop hook is the only signal separating an error / a user-cancel
        # from a normal hand-back — the transcript can't show those.
        if sess.last_hook_status == "error":
            return ("errored", "high", f"{headline} errored.")
        if sess.last_hook_status in ("aborted", "cancelled", "canceled", "stopped"):
            return ("cancelled", "low", f"{headline} was cancelled.")
        # A quiet thread that left a real wrap-up turn has handed back to you.
        if _transcript_handed_back(sess.transcript_path) and sess.last_assistant_text:
            return ("finished", "high", f"{headline} finished and handed back to you.")
        # Quiet, but no wrap-up and no stop ever: a genuine hang is the ONLY way a
        # silently-stuck thread surfaces. Wait out hung_window, then state it
        # factually — never a "definitely stuck" claim.
        if quiet_for >= hung:
            mins = int(quiet_for / 60)
            return (
                "stalled", "high",
                f"{headline} has been quiet for {mins} min with no hand-back (possible hang).",
            )
        return None

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
# Lifecycle-watcher helpers. The hook is a wake + a status HINT; the transcript
# is the source of truth for "did it hand back, and in what state?".
# ---------------------------------------------------------------------------

def _hook_terminal_status(hook_type: str, payload: dict) -> str:
    """The stop hook's status, normalized — the watcher's one disambiguator for
    error vs user-cancel vs a normal hand-back. Non-stop hooks mean the loop is
    still going; the transcript, not the hook, decides when it has settled."""
    if hook_type == "stop":
        status = (payload.get("status") or payload.get("final_status") or "").lower()
        if status in ("error", "aborted", "cancelled", "canceled", "stopped"):
            return status
        return "completed"
    if hook_type == "sessionEnd":
        return "error" if (payload.get("reason") or "").lower() == "error" else "completed"
    return "running"


def _status_for_kind(kind: EventKind, prior: Status) -> Status:
    """Per-thread status after a terminal classification."""
    if kind in ("finished", "cancelled"):
        return "finished"
    if kind == "errored":
        return "errored"
    if kind == "question":
        return "waiting"
    if kind == "stalled":
        return "running"  # still technically running — a hang, not a hand-back
    return prior


def _transcript_handed_back(path: str | None) -> bool:
    """True iff the transcript's last turn is a finished assistant wrap-up — text
    the agent left for you — rather than a dangling tool call still in flight.

    A quiet thread whose last entry is a tool_use (or a tool/user result the
    agent hasn't answered yet) has NOT handed back: it is mid-action or hung.
    This is the shape check that keeps a hung tool from masquerading as 'done'.
    Bounded tail read; runs only at settle, never on the hot poll path.
    """
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-262144, os.SEEK_END)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    last = None
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("role") in ("user", "assistant"):
            last = obj
    if last is None or last.get("role") != "assistant":
        # No parsable tail, or the agent's turn is next -> not a hand-back.
        return False
    message = last.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return False
    for block in reversed(content):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and str(block.get("text", "")).strip():
            return True
        if block.get("type") == "tool_use":
            return False
    return False


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


def _session_live(sess: "SessionInfo", now: float) -> bool:
    """True iff this thread is actively generating RIGHT NOW: its status is
    running AND its last hook landed within the recency window."""
    return sess.status == "running" and (now - sess.last_event_at) <= RUNNING_RECENCY_SEC


def _aggregate_agent_status(agent: "CursorAgent", now: float) -> Status:
    """Workspace status as the aggregate of its threads — running if ANY thread
    is live-running, so one thread finishing/aborting can't flip the whole
    workspace to "finished" while a sibling is still working."""
    sessions = list(agent.sessions.values())
    if not sessions:
        return agent.status
    if any(_session_live(s, now) for s in sessions):
        return "running"
    latest = max(sessions, key=lambda s: s.last_event_at)
    return latest.status if latest.status != "unknown" else agent.status


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


def _guess_claude_transcript_path(workspace_root: str, session_id: str) -> str | None:
    """Expected JSONL path for a Claude Code session.

    Claude Code stores transcripts flat at
    `~/.claude/projects/<munged-cwd>/<session-id>.jsonl`, where the cwd is
    munged by replacing every `/` with `-`. Returns the path only if it
    exists on disk (the driver's stream is the primary state source; this is
    just so `cursor_read` can locate the durable file for a Claude Code thread).
    """
    if not session_id:
        return None
    base_dir = os.path.join(os.path.expanduser("~/.claude"), "projects")

    def _munge(c: str) -> str:
        return c.replace("/", "-")

    candidates = [_munge(workspace_root)]
    real = os.path.realpath(workspace_root) if os.path.exists(workspace_root) else workspace_root
    if real != workspace_root:
        candidates.append(_munge(real))
    for name in candidates:
        jsonl = os.path.join(base_dir, name, f"{session_id}.jsonl")
        if os.path.exists(jsonl):
            return jsonl
    return None


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

cursor_registry = CursorAgentRegistry()
