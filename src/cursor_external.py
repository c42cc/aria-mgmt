"""External-Cursor-window observer.

Aria's eyes on the IDE windows the user opened manually. The user runs two
other Cursor windows; Aria sees what happens in them via:

  1. Cursor user-level hooks  (~/.cursor/hooks.json forwarder POSTs here)
  2. Transcript JSONL tailing (~/.cursor/projects/<safe-cwd>/agent-transcripts/<sid>/<sid>.jsonl)
  3. Plan-file watching       (~/.cursor/plans/*.plan.md)

This module:

- Runs a local-only aiohttp server bound to 127.0.0.1 (no remote access).
- Normalizes incoming hook payloads: workspace_roots -> short project name,
  status -> "interesting" verdict, transcript_path -> last N turns.
- Debounces noisy events so afterAgentResponse doesn't fire the pager
  every turn.
- Hands off to a pager_callback injected by bot.py on startup.

No state lives here that survives a process restart. Recent-event dedup is
in-memory only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from aiohttp import web

from .config import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filter table: which hook events Aria reacts to.
# Each row is (hook_type, predicate, severity_label, brief_template).
# Severity drives the pager rung; brief_template gives the user-visible
# one-liner used for DM body / voice injection.
# ---------------------------------------------------------------------------

INTERESTING = "interesting"
NOISE = "noise"


def _classify(hook_type: str, payload: dict) -> tuple[str, str, str]:
    """Return (interest, severity, brief).

    interest: "interesting" or "noise"
    severity: "high" (page hard), "low" (queue only), "noise"
    brief:    user-visible one-line description
    """
    status = (payload.get("status") or payload.get("final_status") or "").lower()
    tool_name = (
        payload.get("tool_name")
        or (payload.get("tool_call") or {}).get("name")
        or ""
    )
    project = payload.get("_project", "an unknown project")

    if hook_type == "stop":
        if status == "completed":
            return INTERESTING, "high", f"Task completed in {project}."
        if status == "error":
            return INTERESTING, "high", f"Task errored in {project}."
        if status == "aborted":
            return INTERESTING, "low", f"Task aborted in {project}."
        return INTERESTING, "low", f"Agent loop ended in {project} (status={status or 'unknown'})."

    if hook_type == "subagentStop":
        if status == "error":
            return INTERESTING, "high", f"Subagent errored in {project}."
        return INTERESTING, "low", f"Subagent finished in {project} (status={status or 'ok'})."

    if hook_type == "postToolUse":
        if "createplan" in tool_name.lower().replace(" ", "") or "create_plan" in tool_name.lower():
            return INTERESTING, "high", f"Plan constructed in {project}."
        if tool_name.lower() == "task":
            return INTERESTING, "low", f"Subagent dispatched in {project}."
        return NOISE, "noise", ""

    if hook_type == "sessionEnd":
        reason = (payload.get("reason") or "").lower()
        if reason in ("error",):
            return INTERESTING, "high", f"Cursor session ended with error in {project}."
        return NOISE, "noise", ""

    if hook_type == "afterAgentResponse":
        return NOISE, "noise", ""

    return NOISE, "noise", ""


# ---------------------------------------------------------------------------
# Workspace root -> project name resolution
# ---------------------------------------------------------------------------

def _extract_workspace_root(payload: dict) -> str | None:
    """Best-effort: pick a project cwd out of the hook payload."""
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


def resolve_project_name(workspace_root: str | None, registry: dict[str, str]) -> str:
    """Map a workspace cwd to a registered short name. Falls back to basename."""
    if not workspace_root:
        return "unknown"
    norm = workspace_root.rstrip("/")
    for name, path in registry.items():
        if path.rstrip("/") == norm:
            return name
    base = os.path.basename(norm) or norm
    return base


def cursor_project_data_dir(cwd: str) -> str:
    """Compute Cursor's per-project folder under ~/.cursor/projects/.

    Cursor sanitizes the cwd by replacing every "/" and "_" with "-" (and
    stripping the leading slash). So /Users/corbin/PycharmProjects/agi_env_v1/ucs2
    becomes Users-corbin-PycharmProjects-agi-env-v1-ucs2.

    Tricky parts:
      - macOS symlinks `/var` -> `/private/var`. Cursor uses the realpath,
        so `/var/folders/.../T/foo` becomes `private-var-folders-...-T-foo`.
        We try both the literal cwd and its `os.path.realpath` variant.
      - Cursor's encoding rules have changed across versions; we also try
        the no-underscore-replacement variant as a fallback.
    """
    base_dir = os.path.join(config.cursor_user_data_dir, "projects")

    def _sanitize(c: str) -> str:
        return c.lstrip("/").replace("/", "-").replace("_", "-")

    def _sanitize_no_underscore(c: str) -> str:
        return c.lstrip("/").replace("/", "-")

    real = os.path.realpath(cwd) if os.path.exists(cwd) else cwd
    candidates: list[str] = []
    for c in (cwd, real):
        candidates.append(_sanitize(c))
        candidates.append(_sanitize_no_underscore(c))

    seen: set[str] = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        candidate = os.path.join(base_dir, name)
        if os.path.isdir(candidate):
            return candidate

    return os.path.join(base_dir, _sanitize(real))


# ---------------------------------------------------------------------------
# Transcript reader: pull the last N turns from the most recent JSONL.
# ---------------------------------------------------------------------------

def _latest_transcript_path(cwd: str) -> str | None:
    """Find the most-recently-modified transcript JSONL for this project."""
    root = os.path.join(cursor_project_data_dir(cwd), "agent-transcripts")
    if not os.path.isdir(root):
        return None
    candidates: list[tuple[float, str]] = []
    for sid in os.listdir(root):
        sub = os.path.join(root, sid)
        if not os.path.isdir(sub):
            continue
        jsonl = os.path.join(sub, f"{sid}.jsonl")
        if os.path.exists(jsonl):
            try:
                candidates.append((os.path.getmtime(jsonl), jsonl))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def read_last_n_turns(cwd: str, n: int = 3, *, explicit_path: str | None = None) -> list[dict]:
    """Return up to N most-recent role-tagged turns from the JSONL.

    Each turn is `{role, text, has_tool_use}` where text is the first text
    block of that turn truncated to ~1000 chars. has_tool_use flags turns
    that called tools (useful signal for "task in progress" vs. "task
    waiting for input").
    """
    path = explicit_path or _latest_transcript_path(cwd)
    if not path or not os.path.exists(path):
        return []

    turns: list[dict] = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = obj.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                message = obj.get("message") or {}
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                text_parts: list[str] = []
                has_tool_use = False
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        t = block.get("text", "")
                        if isinstance(t, str):
                            text_parts.append(t)
                    elif btype == "tool_use":
                        has_tool_use = True
                if not text_parts and not has_tool_use:
                    continue
                turns.append({
                    "role": role,
                    "text": ("\n".join(text_parts))[:1000],
                    "has_tool_use": has_tool_use,
                })
    except OSError:
        return []

    return turns[-n:]


# ---------------------------------------------------------------------------
# Plan file discovery
# ---------------------------------------------------------------------------

_PLAN_NAME_RE = re.compile(r"^(?P<slug>[a-z0-9_-]+)_(?P<hash>[a-f0-9]+)\.plan\.md$")


def list_recent_plans(*, max_age_sec: int = 600, limit: int = 5) -> list[dict]:
    """Return plan files modified within `max_age_sec`, most recent first.

    Each entry: `{name, path, mtime, slug}`. Reads only directory metadata —
    cheap enough to call per-event.
    """
    plans_dir = os.path.join(config.cursor_user_data_dir, "plans")
    if not os.path.isdir(plans_dir):
        return []
    now = time.time()
    out: list[dict] = []
    try:
        for entry in os.listdir(plans_dir):
            m = _PLAN_NAME_RE.match(entry)
            if not m:
                continue
            path = os.path.join(plans_dir, entry)
            try:
                mt = os.path.getmtime(path)
            except OSError:
                continue
            if now - mt > max_age_sec:
                continue
            out.append({
                "name": entry,
                "path": path,
                "mtime": mt,
                "slug": m.group("slug"),
            })
    except OSError:
        return []
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# Observer: HTTP server + dispatch
# ---------------------------------------------------------------------------

PagerCallback = Callable[["CursorEvent"], Coroutine[Any, Any, None]]


@dataclass
class CursorEvent:
    """Normalized cursor lifecycle event."""

    hook_type: str
    project: str
    workspace_root: str | None
    status: str
    tool_name: str
    severity: str
    brief: str
    raw: dict
    received_at: float = field(default_factory=time.time)
    transcript_snippet: list[dict] = field(default_factory=list)
    recent_plans: list[dict] = field(default_factory=list)


class CursorExternalObserver:
    """Listens for Cursor hook events and routes interesting ones to a pager."""

    def __init__(
        self,
        *,
        pager_callback: PagerCallback,
        registry_provider: Callable[[], dict[str, str]],
        host: str | None = None,
        port: int | None = None,
        debounce_window_sec: float = 8.0,
    ):
        self._pager = pager_callback
        self._registry_provider = registry_provider
        self._host = host or config.cursor_event_host
        self._port = port or config.cursor_event_port
        self._debounce_window = debounce_window_sec
        self._recent: dict[str, float] = {}
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._started_at: float = 0.0
        self._events_seen: int = 0
        self._events_paged: int = 0

    @property
    def alive(self) -> bool:
        return self._runner is not None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/cursor-event"

    @property
    def stats(self) -> dict:
        return {
            "alive": self.alive,
            "url": self.url,
            "started_at": self._started_at,
            "events_seen": self._events_seen,
            "events_paged": self._events_paged,
        }

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_post("/cursor-event", self._handle_event)
        app.router.add_get("/healthz", self._handle_health)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        self._started_at = time.time()
        log.info("Cursor external observer listening on %s", self.url)

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        log.info("Cursor external observer stopped")

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            **self.stats,
        })

    async def _handle_event(self, request: web.Request) -> web.Response:
        if request.remote not in ("127.0.0.1", "::1"):
            return web.Response(status=403, text="local only")

        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid JSON")
        if not isinstance(payload, dict):
            return web.Response(status=400, text="payload must be an object")

        self._events_seen += 1
        asyncio.create_task(self._dispatch(payload))
        return web.json_response({"ok": True})

    async def _dispatch(self, payload: dict) -> None:
        try:
            hook_type = payload.get("_hook_type", "unknown")
            workspace_root = _extract_workspace_root(payload)
            project = resolve_project_name(workspace_root, self._registry_provider())
            payload["_project"] = project
            payload["_workspace_root"] = workspace_root

            interest, severity, brief = _classify(hook_type, payload)
            if interest == NOISE:
                return

            dedup_key = f"{hook_type}:{project}:{payload.get('status', '')}:{payload.get('tool_name', '')}"
            now = time.time()
            last = self._recent.get(dedup_key, 0.0)
            if now - last < self._debounce_window:
                log.debug("Debounced cursor event: %s", dedup_key)
                return
            self._recent[dedup_key] = now
            self._prune_recent(now)

            transcript_snippet: list[dict] = []
            recent_plans: list[dict] = []
            if workspace_root:
                transcript_path = payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None
                transcript_snippet = read_last_n_turns(workspace_root, n=3, explicit_path=transcript_path)
            recent_plans = list_recent_plans(max_age_sec=600, limit=3)

            evt = CursorEvent(
                hook_type=hook_type,
                project=project,
                workspace_root=workspace_root,
                status=str(payload.get("status") or payload.get("final_status") or ""),
                tool_name=str(payload.get("tool_name") or ""),
                severity=severity,
                brief=brief,
                raw=payload,
                transcript_snippet=transcript_snippet,
                recent_plans=recent_plans,
            )

            try:
                await self._pager(evt)
                self._events_paged += 1
            except Exception:
                log.exception("Pager callback raised for %s", dedup_key)
        except Exception:
            log.exception("Cursor event dispatch crashed")

    def _prune_recent(self, now: float) -> None:
        cutoff = now - 600.0
        for k, t in list(self._recent.items()):
            if t < cutoff:
                del self._recent[k]
