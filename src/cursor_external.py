"""External-Cursor observer: HTTP listener + filesystem helpers.

Two roles after the unified-agent-layer migration:

1. Run a local-only aiohttp server that accepts the `~/.cursor/hooks.json`
   forwarder's POSTs and writes them through to `CursorAgentRegistry`.
   The registry is the source of truth — no pager rungs, no `CursorEvent`,
   no in-process narration owned here.

2. Expose filesystem helpers (`cursor_project_data_dir`, `read_last_n_turns`,
   `list_recent_plans`, `_extract_workspace_root`, `resolve_project_name`)
   used by `src/cursor_registry.py`, `src/cursor_tools.py`, and the
   internal IDE-side osascript helpers that still live in `src/tools.py`.

Also serves `POST /aria_say` so test harnesses can ask Aria to speak a
specific line aloud via the live Gemini session.

No state lives here that survives a process restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Callable, Coroutine

from aiohttp import web

from .config import config

log = logging.getLogger(__name__)


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

# Returns a duck-typed voice injector when voice is available, None otherwise.
# The returned object must expose `connected: bool` and
# `async inject_text(text: str, turn_complete: bool) -> None`.
# Typed as Any to avoid a cyclic import on `GeminiSession`.
VoiceInjectorProvider = Callable[[], Any]

# Side-channel write-through into the unified `CursorAgentRegistry`. The
# registry is the source of truth used by Aria's tools and the narrator;
# the observer's only job here is to parse the hook payload and call
# `registry.register_from_hook(hook_type, payload)`.
RegistryWriter = Callable[[str, dict], Coroutine[Any, Any, None]]

# Returns a duck-typed `ConversationBuffer` (or None). The returned object
# must expose `last_n_turns(n: int) -> list[dict]`. Typed as Any to avoid
# a cyclic import on `conversation.ConversationBuffer`.
ConversationProvider = Callable[[], Any]

# Returns a duck-typed `GeminiSession` (or None) regardless of whether
# it is currently connected. Used by the test-only endpoints
# (`/test_connect_gemini`, `/test_voice_in`) so harnesses can force a
# fresh Gemini Live connection without needing the operator to be in
# the Discord voice channel. In production this would be `lambda: gemini`.
GeminiProvider = Callable[[], Any]


class CursorExternalObserver:
    """Local HTTP listener for `~/.cursor/hooks.json` forwarder events.

    Thin write-through to the `CursorAgentRegistry`. All
    classification, severity decisions, narration, and DM routing live
    in `cursor_registry.py` + `bot.py::_narrate_registry_event`.
    """

    def __init__(
        self,
        *,
        registry_provider: Callable[[], dict[str, str]],
        host: str | None = None,
        port: int | None = None,
        voice_injector_provider: VoiceInjectorProvider | None = None,
        registry_writer: RegistryWriter | None = None,
        conversation_provider: ConversationProvider | None = None,
        gemini_provider: GeminiProvider | None = None,
    ):
        self._registry_provider = registry_provider
        self._host = host or config.cursor_event_host
        self._port = port or config.cursor_event_port
        self._voice_injector_provider = voice_injector_provider
        self._registry_writer = registry_writer
        self._conversation_provider = conversation_provider
        self._gemini_provider = gemini_provider
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._started_at: float = 0.0
        self._events_seen: int = 0
        self._events_dispatched: int = 0
        self._aria_say_calls: int = 0
        self._recent_turns_calls: int = 0
        self._test_voice_in_calls: int = 0
        self._test_connect_gemini_calls: int = 0

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
            "events_dispatched": self._events_dispatched,
            "aria_say_calls": self._aria_say_calls,
            "recent_turns_calls": self._recent_turns_calls,
            "test_voice_in_calls": self._test_voice_in_calls,
            "test_connect_gemini_calls": self._test_connect_gemini_calls,
            "voice_injector_wired": self._voice_injector_provider is not None,
            "conversation_provider_wired": self._conversation_provider is not None,
            "gemini_provider_wired": self._gemini_provider is not None,
        }

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application()
        app.router.add_post("/cursor-event", self._handle_event)
        app.router.add_post("/aria_say", self._handle_aria_say)
        app.router.add_get("/recent_turns", self._handle_recent_turns)
        app.router.add_post("/test_connect_gemini", self._handle_test_connect_gemini)
        app.router.add_post("/test_voice_in", self._handle_test_voice_in)
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

    async def _handle_recent_turns(self, request: web.Request) -> web.Response:
        """Return the last N turns from Aria's conversation buffer.

        Localhost-only. Used by `scripts/e2e_aria_golden.py` and other test
        harnesses to assert on what Aria actually heard and said after a
        `/aria_say` injection or a real voice turn. Returns the same Turn
        shape `ConversationBuffer.last_n_turns()` produces: a list of
        `{role, medium, channel, text, ts}` dicts ordered oldest→newest.

        Query params:
        - `n` (default 8, max 60): how many recent turns to return.

        Status codes:
        - 200: turns returned (possibly empty list).
        - 403: non-loopback caller.
        - 503: no `conversation_provider` wired into the observer.
        """
        if request.remote not in ("127.0.0.1", "::1"):
            return web.Response(status=403, text="local only")

        if self._conversation_provider is None:
            return web.Response(
                status=503,
                text="no conversation_provider wired into observer",
            )

        try:
            n = int(request.query.get("n", "8"))
        except ValueError:
            return web.Response(status=400, text="'n' must be an integer")
        n = max(1, min(n, 60))

        try:
            buf = self._conversation_provider()
        except Exception:
            log.exception("conversation_provider raised")
            return web.Response(status=500, text="conversation provider raised")

        if buf is None:
            return web.json_response({"turns": [], "count": 0})

        try:
            turns = buf.last_n_turns(n)
        except Exception:
            log.exception("last_n_turns raised")
            return web.Response(status=500, text="last_n_turns raised")

        self._recent_turns_calls += 1
        return web.json_response({"turns": turns, "count": len(turns)})

    async def _handle_test_connect_gemini(self, request: web.Request) -> web.Response:
        """Force-connect Gemini Live regardless of Discord voice state.

        Test-only. Used by `scripts/e2e_aria_golden.py --tts` so a fully
        autonomous run can exercise the voice path without an operator
        in the Discord voice channel.

        Idempotent. If `gemini.connected` is already True, returns 200
        immediately. Otherwise calls `gemini.connect()` (synchronously
        awaited) and returns 200 on success, 5xx on failure.

        Returns 503 if no `gemini_provider` is wired into the observer.
        """
        if request.remote not in ("127.0.0.1", "::1"):
            return web.Response(status=403, text="local only")
        if self._gemini_provider is None:
            return web.Response(
                status=503, text="no gemini_provider wired into observer",
            )
        try:
            gemini = self._gemini_provider()
        except Exception:
            log.exception("gemini_provider raised in test_connect_gemini")
            return web.Response(status=500, text="gemini_provider raised")
        if gemini is None:
            return web.Response(status=503, text="gemini provider returned None")

        if getattr(gemini, "connected", False):
            self._test_connect_gemini_calls += 1
            return web.json_response({"ok": True, "already_connected": True})

        try:
            await gemini.connect()
        except Exception as exc:
            log.exception("gemini.connect() raised in test_connect_gemini")
            return web.Response(status=500, text=f"gemini.connect failed: {exc}")

        if not getattr(gemini, "connected", False):
            return web.Response(
                status=502, text="gemini.connect returned but `connected` is still False",
            )

        self._test_connect_gemini_calls += 1
        return web.json_response({"ok": True, "already_connected": False})

    async def _handle_test_voice_in(self, request: web.Request) -> web.Response:
        """Synthesize text via TTS and feed it into Gemini Live as user audio.

        Test-only. Mimics what the Discord voice sidecar would feed Aria
        if an operator were speaking the text aloud. Returns 200 after
        the audio (plus trailing silence to trigger Gemini's VAD turn
        completion) has been queued — does NOT wait for Aria's reply.

        Body: `{"text": str, "engine": "gemini"|"say" (default gemini),
                "voice": str (default "Kore"), "trailing_silence_ms": int (default 600)}`
        - 200: queued. Body includes `{ok, pcm_bytes, chunks, duration_sec}`.
        - 400: bad payload.
        - 403: non-loopback.
        - 503: no gemini_provider wired OR Gemini not connected. Caller
                should POST /test_connect_gemini first.
        - 5xx: TTS or send_audio raised.
        """
        if request.remote not in ("127.0.0.1", "::1"):
            return web.Response(status=403, text="local only")
        if self._gemini_provider is None:
            return web.Response(
                status=503, text="no gemini_provider wired into observer",
            )

        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid JSON")
        if not isinstance(payload, dict):
            return web.Response(status=400, text="payload must be an object")

        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return web.Response(status=400, text="missing or empty 'text'")
        engine = payload.get("engine", "gemini")
        if engine not in ("gemini", "say"):
            return web.Response(status=400, text="engine must be 'gemini' or 'say'")
        voice = payload.get("voice", "Kore")
        try:
            trailing_silence_ms = int(payload.get("trailing_silence_ms", 1200))
        except (TypeError, ValueError):
            return web.Response(status=400, text="trailing_silence_ms must be an integer")

        try:
            gemini = self._gemini_provider()
        except Exception:
            log.exception("gemini_provider raised in test_voice_in")
            return web.Response(status=500, text="gemini_provider raised")
        if gemini is None or not getattr(gemini, "connected", False):
            return web.Response(
                status=503,
                text="gemini not currently connected (POST /test_connect_gemini first)",
            )

        # Synthesize on a worker thread so we don't block the event loop.
        try:
            from .test_audio import synthesize_to_pcm, chunk_pcm, silence_pcm
            pcm = await asyncio.get_event_loop().run_in_executor(
                None, lambda: synthesize_to_pcm(text, engine=engine, voice=voice),
            )
        except Exception as exc:
            log.exception("TTS synthesis failed in test_voice_in")
            return web.Response(status=500, text=f"TTS failed: {exc}")

        # Stream chunks to Gemini at ~realtime pacing so the VAD doesn't
        # think the user took an enormous breath. 20 ms chunks match what
        # the production voice bridge feeds the model.
        chunks = list(chunk_pcm(pcm, chunk_ms=20))
        chunk_count = len(chunks)
        try:
            for chunk in chunks:
                await gemini.send_audio(chunk)
                await asyncio.sleep(0.018)  # slightly faster than realtime
            # Trailing silence so VAD has clear speech-to-silence transition.
            if trailing_silence_ms > 0:
                silence = silence_pcm(trailing_silence_ms)
                for chunk in chunk_pcm(silence, chunk_ms=20):
                    await gemini.send_audio(chunk)
                    await asyncio.sleep(0.018)
            # Explicitly signal end of audio so Gemini responds without
            # waiting for VAD timeout. Without this, a finite TTS
            # utterance can sit in the buffer indefinitely and Aria
            # never replies (observed during golden-path early runs).
            signal_audio_end = getattr(gemini, "signal_audio_end", None)
            if callable(signal_audio_end):
                await signal_audio_end()
        except Exception as exc:
            log.exception("gemini.send_audio raised in test_voice_in")
            return web.Response(status=500, text=f"send_audio failed: {exc}")

        self._test_voice_in_calls += 1
        duration_sec = len(pcm) / (16_000 * 2)
        return web.json_response({
            "ok": True,
            "pcm_bytes": len(pcm),
            "chunks": chunk_count,
            "duration_sec": round(duration_sec, 3),
            "trailing_silence_ms": trailing_silence_ms,
            "engine": engine,
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

    async def _handle_aria_say(self, request: web.Request) -> web.Response:
        """Inject a narration line into the live Gemini session.

        Localhost-only. Used by test harnesses (scripts/e2e_aria_narrates.py)
        and any other local driver that wants Aria to speak a specific line
        aloud over the voice channel.

        Body: {"text": "...", "turn_complete": bool (default true)}
        - 200: queued for Gemini.
        - 400: malformed payload.
        - 403: non-loopback caller.
        - 503: no voice injector wired, or Gemini not currently connected.
                The caller should treat this as "voice unavailable right now"
                and fail loud rather than silently dropping the line.
        """
        if request.remote not in ("127.0.0.1", "::1"):
            return web.Response(status=403, text="local only")

        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400, text="invalid JSON")
        if not isinstance(payload, dict):
            return web.Response(status=400, text="payload must be an object")

        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return web.Response(status=400, text="missing or empty 'text'")

        turn_complete = payload.get("turn_complete", True)
        if not isinstance(turn_complete, bool):
            return web.Response(status=400, text="'turn_complete' must be a boolean")

        if self._voice_injector_provider is None:
            return web.Response(
                status=503,
                text="no voice_injector_provider wired into observer",
            )

        try:
            injector = self._voice_injector_provider()
        except Exception:
            log.exception("voice_injector_provider raised")
            return web.Response(status=500, text="voice provider raised")

        if injector is None or not getattr(injector, "connected", False):
            return web.Response(
                status=503,
                text="voice unavailable (Gemini not currently connected)",
            )

        # Defer until Gemini is idle. Without this, an inject sent while the
        # model is still producing an in-flight turn (typically: right after
        # the bot auto-joins voice and Aria is mid-greeting/preamble) gets
        # batched into the in-progress generation and silently dropped. The
        # observable failure mode is "/aria_say returned 200 but nothing was
        # spoken." 15s is enough for any normal preamble; on timeout we
        # inject anyway and log so callers can still see something landed.
        wait_until_idle = getattr(injector, "wait_until_idle", None)
        if callable(wait_until_idle):
            try:
                idle = await wait_until_idle(timeout=15.0)
            except Exception:
                log.exception("wait_until_idle raised on /aria_say — proceeding with inject")
                idle = True
            if not idle:
                log.warning(
                    "/aria_say timed out waiting for Gemini idle after 15s — "
                    "injecting anyway (may be batched into in-flight turn)"
                )

        try:
            await injector.inject_text(text, turn_complete=turn_complete)
        except Exception:
            log.exception("inject_text failed on /aria_say")
            return web.Response(status=500, text="inject_text failed")

        self._aria_say_calls += 1
        return web.json_response({"ok": True, "chars": len(text)})

    async def _dispatch(self, payload: dict) -> None:
        """Decorate the payload and hand off to the registry write-through.

        The registry (`cursor_registry.CursorAgentRegistry`) owns the
        classification, status transitions, transcript tailing, and
        narration emit. This function is intentionally tiny.
        """
        try:
            hook_type = payload.get("_hook_type", "unknown")
            workspace_root = _extract_workspace_root(payload)
            project = resolve_project_name(workspace_root, self._registry_provider())
            payload["_project"] = project
            payload["_workspace_root"] = workspace_root

            if self._registry_writer:
                try:
                    await self._registry_writer(hook_type, payload)
                    self._events_dispatched += 1
                except Exception:
                    log.exception("registry_writer raised on hook=%s", hook_type)
        except Exception:
            log.exception("Cursor event dispatch crashed")
