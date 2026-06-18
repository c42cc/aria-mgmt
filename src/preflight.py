"""Preflight capability probes.

Every advertised capability runs one real end-to-end probe.
If a critical probe fails, the bot refuses to enter ready state.

Each probe returns a ProbeResult that carries:
  - the actual error (no swallowing)
  - the exact fix command (no guessing)
  - a severity (critical/warn/info)

Run as CLI: `python -m src.preflight` (exits non-zero on critical failure).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


CRITICAL = "critical"
WARN = "warn"
INFO = "info"


@dataclass
class ProbeResult:
    """The result of one capability probe."""

    name: str
    ok: bool
    severity: str
    error: str = ""
    fix_command: str = ""
    detail: str = ""
    elapsed_ms: int = 0


@dataclass
class PreflightReport:
    """Aggregate result of all probes."""

    results: list[ProbeResult] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def critical_failures(self) -> list[ProbeResult]:
        return [r for r in self.results if not r.ok and r.severity == CRITICAL]

    @property
    def warnings(self) -> list[ProbeResult]:
        return [r for r in self.results if not r.ok and r.severity == WARN]

    @property
    def passed(self) -> list[ProbeResult]:
        return [r for r in self.results if r.ok]

    @property
    def ok(self) -> bool:
        return not self.critical_failures


# ---------------------------------------------------------------------------
# Probe runner: wraps a coroutine, catches every exception, times it
# ---------------------------------------------------------------------------


async def _run_probe(
    name: str,
    severity: str,
    coro_factory: Callable[[], Awaitable[tuple[bool, str, str, str]]],
) -> ProbeResult:
    """Execute a probe coroutine and produce a ProbeResult.

    The probe coroutine must return (ok, error, fix_command, detail).
    Any exception becomes ok=False with error=str(exc).
    """
    start = time.monotonic()
    try:
        ok, error, fix_command, detail = await coro_factory()
    except Exception as exc:
        ok, error, fix_command, detail = False, f"{type(exc).__name__}: {exc}", "", ""
    elapsed_ms = int((time.monotonic() - start) * 1000)
    return ProbeResult(
        name=name,
        ok=ok,
        severity=severity,
        error=error,
        fix_command=fix_command,
        detail=detail,
        elapsed_ms=elapsed_ms,
    )


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


async def probe_config() -> tuple[bool, str, str, str]:
    """All required env vars are present."""
    from .config import config

    missing = []
    if not config.discord_bot_token:
        missing.append("DISCORD_APP_BOT_TOKEN")
    if not config.google_api_key:
        missing.append("GEMINI_API_KEY")
    if not config.anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    if not config.discord_text_channel_id:
        missing.append("DISCORD_TEXT_CHANNEL_ID")
    if not config.discord_log_channel_id:
        missing.append("DISCORD_LOG_CHANNEL_ID")
    if not config.authorized_user_ids:
        missing.append("AUTHORIZED_USER_IDS")
    if missing:
        return False, f"Missing env vars: {', '.join(missing)}", "Edit .env", ""
    return True, "", "", f"all keys present, models: gemini={config.gemini_model} claude={config.claude_model}"


async def probe_db() -> tuple[bool, str, str, str]:
    """DB initializes and supports write+read round-trip."""
    from .db import init_db, get_connection, log_event, get_daily_spend

    init_db()
    log_event("preflight_probe", {"ts": time.time()}, "ok", 0, "preflight", 0.0)
    spend = get_daily_spend()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) as c FROM events WHERE tool_name='preflight_probe'"
        ).fetchone()
    return True, "", "", f"events table OK, spend=${spend:.4f}, probe_rows={rows['c']}"


async def probe_anthropic() -> tuple[bool, str, str, str]:
    """Anthropic API reachable with a 1-token completion."""
    from .config import config
    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    resp = await asyncio.to_thread(
        client.messages.create,
        model=config.claude_model,
        max_tokens=4,
        messages=[{"role": "user", "content": "ping"}],
    )
    text = resp.content[0].text if resp.content else ""
    return True, "", "", f"model={config.claude_model} usage={getattr(resp, 'usage', None)} text={text[:30]!r}"


async def probe_gemini_audio() -> tuple[bool, str, str, str]:
    """Gemini Live actually EMITS audio bytes for a short prompt (not just
    connects). This is the probe whose absence let 'voice doesn't work' hide
    for months: connect succeeded while the model produced no audio."""
    from .config import config
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.google_api_key)
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore"))),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    audio = bytearray()
    tx = ""
    async with client.aio.live.connect(model=config.gemini_model, config=cfg) as s:
        await s.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text="Say exactly: voice check.")]),
            turn_complete=True,
        )

        async def collect():
            nonlocal tx
            async for msg in s.receive():
                sc = msg.server_content
                if not sc:
                    continue
                if sc.model_turn:
                    for p in sc.model_turn.parts:
                        if p.inline_data and p.inline_data.data:
                            audio.extend(p.inline_data.data)
                if sc.output_transcription and sc.output_transcription.text:
                    tx += sc.output_transcription.text
                if getattr(sc, "turn_complete", False):
                    return

        try:
            await asyncio.wait_for(collect(), timeout=25)
        except asyncio.TimeoutError:
            pass
    if len(audio) < 4000:  # < ~80ms of 24k s16le mono => effectively no voice
        return (
            False,
            f"Gemini Live emitted only {len(audio)} audio bytes for a say-hello",
            "Check GEMINI_MODEL (demand-throttling?) — model produced no/insufficient audio",
            f"model={config.gemini_model} tx={tx[:40]!r}",
        )
    return True, "", "", (
        f"model={config.gemini_model} emitted {len(audio)} bytes "
        f"(~{len(audio)/48000:.1f}s) tx={tx[:30]!r}"
    )


async def probe_gemini_connect() -> tuple[bool, str, str, str]:
    """Gemini Live WebSocket connects and closes cleanly."""
    from .gemini_session import GeminiSession

    gs = GeminiSession()
    try:
        await gs.connect()
        if not gs.connected:
            return False, "GeminiSession.connect() returned without setting connected=True", "", ""
        await gs.close()
        return True, "", "", "connect+close round-trip OK"
    except Exception as exc:
        # Re-raise so the wrapper captures it cleanly
        try:
            await gs.close()
        except Exception:
            pass
        raise


async def probe_mem0() -> tuple[bool, str, str, str]:
    """mem0 remember+recall round-trip succeeds."""
    from .memory import init_memory, remember, recall

    init_memory()
    sentinel = f"preflight-{int(time.time())}"
    remember(f"Preflight sentinel: {sentinel}")
    results = recall("preflight sentinel")
    if not results:
        return False, "recall returned empty after remember", "Check Gemini API key and mem0 vector store at data/mem0/", ""
    return True, "", "", f"recalled {len(results)} memories"


async def probe_discord_post(post_callback: Callable | None) -> tuple[bool, str, str, str]:
    """Posting to #ucs-alerts actually delivers."""
    if post_callback is None:
        return False, "no alert post_callback wired", "Check bot.py on_ready wiring", ""
    await post_callback(f"`preflight ping {int(time.time())}`")
    return True, "", "", "alert channel reachable"


async def probe_mcp_server(mcp_client: Any, server_name: str, severity: str) -> tuple[bool, str, str, str]:
    """An MCP server is running and has at least one tool."""
    if mcp_client is None or not getattr(mcp_client, "_started", False):
        return False, "MCP client not started", "Check bot.py on_ready MCP init", ""
    if server_name not in mcp_client._servers:
        return False, f"server '{server_name}' not connected", f"Check npx availability and {server_name} package", ""
    tools = [t for t in mcp_client._tools if mcp_client._tool_to_server.get(t) == server_name]
    if not tools:
        return False, f"server '{server_name}' connected but offers no tools", "", ""
    return True, "", "", f"{len(tools)} tools available"


async def probe_mcp_apple_calendar(mcp_client: Any) -> tuple[bool, str, str, str]:
    """Apple MCP can actually read calendars (verifies EventKitCLI binary + permissions).

    A1: explicitly checks for the `write-only` mode that produced 21 audit
    hits between 2026-05-12 and 2026-05-16 without ever turning into a
    preflight failure. The string is canonical in mcp-macos output.

    Args: the mcp-macos package collapsed every calendar verb into a single
    tool gated by `action`. Calling with `{}` produces an arg-schema
    violation. We pass `action: "list"` (the read-only variant) and fall
    back to schema introspection if the default value is rejected by a
    future schema bump.
    """
    if mcp_client is None or "apple" not in mcp_client._servers:
        return False, "apple MCP not running", "ops/bootstrap.sh", ""

    args = {"action": "list"}
    schema = (mcp_client._tools.get("calendar_calendars") or {}).get("input_schema") or {}
    enum_values: list[str] = []
    try:
        action_prop = (schema.get("properties") or {}).get("action") or {}
        enum_values = list(action_prop.get("enum") or [])
        if enum_values and "list" not in enum_values:
            # Schema bumped — pick a read-like verb if any, else first value.
            for candidate in ("list", "read", "get", "fetch", "all"):
                if candidate in enum_values:
                    args = {"action": candidate}
                    break
            else:
                args = {"action": enum_values[0]}
    except Exception:
        pass

    result = await mcp_client.call_tool("calendar_calendars", args)
    text = str(result)
    lower = text.lower()
    if "EventKitCLI binary not found" in text:
        return (
            False,
            "EventKitCLI Swift binary missing in mcp-macos package",
            "bash ops/build_macos_swift.sh",
            text[:200],
        )
    if "write-only" in lower:
        return (
            False,
            "macOS Calendar permission is WRITE-ONLY; read access required",
            "System Settings > Privacy & Security > Calendars > toggle Aria's host to Full Access",
            text[:200],
        )
    if "permission" in lower or "not authorized" in lower or "TCC" in text:
        return (
            False,
            "macOS Calendar permission not granted",
            "bash ops/grant_permissions.sh",
            text[:200],
        )
    if "arg-schema violation" in lower or "missing required argument" in lower:
        hint = f"valid action values: {enum_values}" if enum_values else "inspect calendar_calendars input_schema"
        return False, "calendar_calendars rejected our args", hint, text[:300]
    if "error" in lower[:40] or "failed" in lower[:40]:
        return False, "calendar_calendars call failed", "", text[:300]
    return True, "", "", f"action={args['action']!r} got {text[:100]!r}"


async def probe_mcp_time(mcp_client: Any) -> tuple[bool, str, str, str]:
    """A1: the `google-calendar.get-current-time` MCP returns a parseable ISO timestamp.

    P3 already removes Aria's runtime dependence on this tool (date comes
    from the host context block), but the probe is a smoke test: if the
    google-calendar MCP is healthy, time should work too. Severity WARN.
    """
    if mcp_client is None:
        return False, "MCP client not started", "", ""
    if "google-calendar" not in mcp_client._servers:
        return False, "google-calendar MCP not running", "ops/google_oauth_bootstrap.py", ""
    if "get-current-time" not in mcp_client._tools and "get_current_time" not in mcp_client._tools:
        return True, "", "", "tool absent — not all gcal servers expose it"
    tool = "get-current-time" if "get-current-time" in mcp_client._tools else "get_current_time"
    try:
        result = await mcp_client.call_tool(tool, {})
        text = str(result)
        # Accept anything that contains an ISO-shaped fragment.
        if re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text):
            return True, "", "", f"got {text[:80]}"
        return False, f"{tool} returned no parseable ISO timestamp", "", text[:200]
    except Exception as e:
        return False, f"{tool} raised: {e}", "", ""


async def probe_mcp_tool_name_regex(mcp_client: Any) -> tuple[bool, str, str, str]:
    """A1: every registered MCP tool name matches Anthropic's `^[a-zA-Z0-9_-]{1,128}$` regex.

    Session 24 was killed at the API level because one tool slipped through
    name sanitization. This is the belt-and-suspenders check; P2(a) is the
    runtime gate. Severity CRITICAL.
    """
    if mcp_client is None or not getattr(mcp_client, "_started", False):
        return False, "MCP client not started", "", ""
    pattern = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
    offenders = [n for n in mcp_client._tools if not pattern.match(n)]
    if offenders:
        return (
            False,
            f"{len(offenders)} tool name(s) do not match Anthropic regex",
            "Check src/mcp.py:_sanitize_tool_name registration path",
            "; ".join(offenders[:5]),
        )
    return True, "", "", f"all {len(mcp_client._tools)} tool names valid"


async def probe_mcp_filesystem(mcp_client: Any) -> tuple[bool, str, str, str]:
    """Filesystem MCP can list a real directory."""
    if mcp_client is None or "filesystem" not in mcp_client._servers:
        return False, "filesystem MCP not running", "ops/bootstrap.sh", ""
    result = await mcp_client.call_tool("list_directory", {"path": "/Users/corbin/Documents"})
    text = str(result)
    if "error" in text.lower()[:40]:
        return False, "list_directory failed", "", text[:300]
    return True, "", "", f"listed Documents ({len(text)} chars)"


async def probe_mcp_shell(mcp_client: Any) -> tuple[bool, str, str, str]:
    """Shell MCP can execute echo."""
    if mcp_client is None or "shell" not in mcp_client._servers:
        return False, "shell MCP not running", "ops/bootstrap.sh", ""
    shell_tools = [t for t in mcp_client._tools if mcp_client._tool_to_server.get(t) == "shell"]
    if not shell_tools:
        return False, "shell server has no tools", "", ""
    result = await mcp_client.call_tool(shell_tools[0], {"command": "echo PREFLIGHT_PING"})
    if "PREFLIGHT_PING" not in str(result):
        return False, f"echo did not round-trip: {str(result)[:200]}", "", ""
    return True, "", "", f"shell tool={shell_tools[0]}"


async def probe_mcp_github(mcp_client: Any) -> tuple[bool, str, str, str]:
    """GitHub MCP exposes tools (deeper auth probe is too expensive at boot)."""
    return await probe_mcp_server(mcp_client, "github", WARN)


async def probe_mcp_gcal(mcp_client: Any) -> tuple[bool, str, str, str]:
    """Google Calendar MCP server connected. Tests OAuth state on first real call."""
    if mcp_client is None or "google-calendar" not in mcp_client._servers:
        return (
            False,
            "google-calendar MCP not running (likely OAuth keys missing)",
            ".venv/bin/python ops/google_oauth_bootstrap.py",
            "Place gcp-oauth.keys.json in ~/.config/google-calendar-mcp/",
        )
    tools = [t for t in mcp_client._tools if mcp_client._tool_to_server.get(t) == "google-calendar"]
    return True, "", "", f"{len(tools)} tools, OAuth verified"


async def probe_mcp_gmail(mcp_client: Any) -> tuple[bool, str, str, str]:
    """Gmail MCP server connected and can actually search mail (not just connect)."""
    if mcp_client is None or "gmail" not in mcp_client._servers:
        return (
            False,
            "gmail MCP not running (OAuth keys missing or npm package not installed)",
            ".venv/bin/python scripts/gmail_oauth.py",
            "Run the OAuth bootstrap to grant Gmail API access",
        )
    gmail_tools = [t for t in mcp_client._tools if mcp_client._tool_to_server.get(t) == "gmail"]
    if not gmail_tools:
        return False, "gmail server connected but has no tools", "", ""

    search_tool = None
    for t in gmail_tools:
        if "search" in t.lower() or "list" in t.lower() or "inbox" in t.lower():
            search_tool = t
            break

    if not search_tool:
        return False, "gmail has no search/list tool — cannot verify live access", "", f"tools: {gmail_tools[:5]}"

    try:
        result = await mcp_client.call_tool(search_tool, {"query": "newer_than:7d", "maxResults": 3})
        text = str(result)
        if "error" in text.lower()[:100] or "no access" in text.lower()[:100]:
            return False, "Gmail auth failed — re-run OAuth", ".venv/bin/python scripts/gmail_oauth.py", text[:200]
        if not text.strip() or text.strip() == "[]" or "no messages" in text.lower()[:100]:
            return False, "Gmail returned empty results for last 7 days — OAuth may be for wrong account", "", text[:200]
        return True, "", "", f"Gmail live — {text[:120]}"
    except Exception as e:
        return False, f"Gmail probe failed: {e}", "", ""


async def probe_anchor_smoke() -> tuple[bool, str, str, str]:
    """Verify that anchor dependencies (Gmail API, Calendar API, filesystem, GitHub) are reachable.

    Fast health check (~5s). An ImportError here means the anchor module is
    broken (e.g. a missing dependency) — surface it loudly rather than
    masking it as "skipped." Live-API reachability per anchor is still
    bounded by per-anchor timeouts and downgraded to WARN at probe level.
    """
    try:
        from .anchors.gmail import GmailSearchAnchor
        from .anchors.calendar_google import GoogleCalendarAnchor
        from .anchors.filesystem import FilesystemSearchAnchor
        from .anchors.github_anchor import GithubAnchor
    except ImportError as e:
        return (
            False,
            f"anchor module failed to import: {e}",
            ".venv/bin/pip install -e . && python -c 'from src.anchors import gmail'",
            "Anchor smoke probe cannot run; the judge floor will be disabled.",
        )

    anchors = {
        "gmail": GmailSearchAnchor(),
        "calendar": GoogleCalendarAnchor(),
        "filesystem": FilesystemSearchAnchor(),
        "github": GithubAnchor(),
    }

    results = {}
    for name, anchor in anchors.items():
        try:
            ok = await asyncio.wait_for(anchor.health_check(), timeout=5)
            results[name] = "ok" if ok else "down"
        except asyncio.TimeoutError:
            results[name] = "timeout"
        except Exception as e:
            results[name] = f"err:{str(e)[:30]}"

    healthy = [k for k, v in results.items() if v == "ok"]
    summary = ", ".join(f"{k}={v}" for k, v in results.items())
    if not healthy:
        return False, "no anchor deps reachable", "", summary
    return True, "", "", f"{len(healthy)}/{len(anchors)} anchors: {summary}"


async def probe_cursor_bridge(cursor_bridge: Any) -> tuple[bool, str, str, str]:
    """Cursor bridge subprocess alive and responds to a ping."""
    if cursor_bridge is None or not cursor_bridge.alive:
        return False, "cursor bridge subprocess not alive", "Check Node.js install and cursor_wrapper/", ""
    # Use the ping action (added in Phase 3)
    try:
        result = await asyncio.wait_for(cursor_bridge.ping(), timeout=5)
        if not result.get("ok"):
            return False, f"ping returned: {result}", "", ""
        return True, "", "", f"pid={cursor_bridge._process.pid} ping ok"
    except AttributeError:
        return False, "cursor_bridge.ping() not implemented", "Update cursor_wrapper to support ping", ""
    except asyncio.TimeoutError:
        return False, "ping timed out after 5s", "", ""


async def probe_dep_drift() -> tuple[bool, str, str, str]:
    """Installed deps match declarations; critical libs are the right flavour."""
    issues: list[str] = []

    # pip check: detects version conflicts among installed packages
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "check",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode().strip()
        if proc.returncode != 0:
            issues.append(f"pip check: {output[:300]}")
    except Exception as exc:
        issues.append(f"pip check failed to run: {exc}")

    # discord must be py-cord, not discord.py
    try:
        import discord
        discord_path = getattr(discord, "__file__", "") or ""
        # py-cord installs as `discord` but the dist-info dir name differs
        # Check for py-cord-specific submodule
        try:
            from discord.sinks.core import Sink  # py-cord only
        except ImportError:
            issues.append(
                "discord.py is installed but py-cord is required for voice receive"
            )
    except Exception as exc:
        issues.append(f"discord import failed: {exc}")

    # numpy must be <2 (PyTorch chain requires it; transformers C-extensions need it)
    try:
        import numpy
        major = int(numpy.__version__.split(".")[0])
        if major >= 2:
            issues.append(f"numpy {numpy.__version__} is >=2; transformers chain requires <2")
    except Exception as exc:
        issues.append(f"numpy import failed: {exc}")

    # mcp Python SDK
    try:
        import mcp  # noqa: F401
    except Exception as exc:
        issues.append(f"mcp SDK import failed: {exc}")

    if issues:
        return (
            False,
            "; ".join(issues),
            ".venv/bin/pip install -e . && bash ops/bootstrap.sh",
            "",
        )
    return True, "", "", "pip check clean, py-cord present, numpy<2"


async def probe_deployed_trunk() -> tuple[bool, str, str, str]:
    """The running process IS the pinned trunk: on branch `main`, the build tree
    clean, and the build unchanged since boot. CRITICAL — a drifted process
    refuses ready instead of lying green.

    Replaces the old WARN `running_code` sentinel, which self-laundered: it
    re-wrote the sentinel to whatever was running on every call and prescribed
    "Restart: make run", so a restart on a feature branch turned the warning
    green. This compares against the trunk (`main`) and a boot hash frozen once
    per process (`build_hash.stamp_boot`), so neither a feature branch nor a
    post-boot edit can re-bless itself.
    """
    from . import build_hash

    live = build_hash.compute_build_hash()
    boot = build_hash.boot_hash()
    if boot is not None and boot != live:
        return (
            False,
            f"source changed since process started (boot={boot[:12]} now={live[:12]})",
            f"git checkout {build_hash.TRUNK} && git pull --ff-only && make run",
            "",
        )
    branch = build_hash.current_branch()
    if branch != build_hash.TRUNK:
        return (
            False,
            f"running branch '{branch}', not the pinned trunk '{build_hash.TRUNK}'",
            f"git checkout {build_hash.TRUNK} && git pull --ff-only && make run",
            f"sha={live[:12]}",
        )
    dirty = build_hash.build_tree_dirty()
    if dirty:
        return (
            False,
            "uncommitted changes to build files (running != committed source)",
            "commit or stash the build changes, then: make run",
            dirty.splitlines()[0],
        )
    return True, "", "", f"trunk={branch} sha={live[:12]}"


async def probe_wake_word_model() -> tuple[bool, str, str, str]:
    """OpenWakeWord model loads without error."""
    try:
        from openwakeword.model import Model
        m = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        return True, "", "", f"loaded hey_jarvis, {len(m.models)} model(s)"
    except ImportError:
        return (
            False,
            "openwakeword not installed",
            ".venv/bin/pip install -e .",
            "",
        )
    except Exception as exc:
        return (
            False,
            f"wake word model failed to load: {exc}",
            ".venv/bin/pip install openwakeword>=0.6",
            "",
        )


async def probe_accessibility() -> tuple[bool, str, str, str]:
    """macOS Accessibility permission works (needed for keystroke paste)."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e",
        'tell application "System Events" to get name of first process whose frontmost is true',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode().strip()
        if "-1743" in err or "not allowed assistive access" in err.lower():
            return (
                False,
                "Accessibility permission denied for System Events",
                "System Settings → Privacy & Security → Accessibility → grant your terminal app",
                err[:200],
            )
        return False, f"osascript failed: {err[:200]}", "", ""
    app_name = stdout.decode().strip()
    return True, "", "", f"frontmost={app_name}"


async def probe_cursor_observer(cursor_observer: Any) -> tuple[bool, str, str, str]:
    """External cursor observer HTTP endpoint is reachable from localhost."""
    if cursor_observer is None:
        return (
            False,
            "External Cursor observer not constructed",
            "Check bot.py on_ready (cursor_observer init); see ARCHITECTURE.md External Cursor Observer",
            "",
        )
    if not cursor_observer.alive:
        return (
            False,
            "External Cursor observer not running (port bind failed or stopped)",
            "Restart bot; check UCS_CURSOR_EVENT_PORT in .env",
            "",
        )
    healthz = cursor_observer.url.rsplit("/", 1)[0] + "/healthz"
    import urllib.request
    try:
        body = await asyncio.to_thread(
            lambda: urllib.request.urlopen(healthz, timeout=2).read().decode()
        )
    except Exception as exc:
        return (
            False,
            f"GET {healthz} failed: {exc}",
            "Restart bot; ensure UCS_CURSOR_EVENT_HOST=127.0.0.1 and port is free",
            "",
        )
    return True, "", "", body[:200]


async def probe_cursor_hooks_installed() -> tuple[bool, str, str, str]:
    """~/.cursor/hooks.json contains the aria-forwarder entries."""
    hooks_path = os.path.expanduser("~/.cursor/hooks.json")
    if not os.path.exists(hooks_path):
        return (
            False,
            f"{hooks_path} does not exist",
            ".venv/bin/python hooks/install.py",
            "",
        )
    try:
        with open(hooks_path) as f:
            data = json.load(f)
    except Exception as exc:
        return (
            False,
            f"{hooks_path} is not valid JSON: {exc}",
            "Fix the hooks file by hand or run: .venv/bin/python hooks/install.py",
            "",
        )

    sections = (data or {}).get("hooks") or {}
    aria_count = 0
    aria_events: list[str] = []
    for event, entries in sections.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("_tag") == "aria-cursor-event":
                aria_count += 1
                aria_events.append(event)

    if aria_count == 0:
        return (
            False,
            "no aria-cursor-event entries found in ~/.cursor/hooks.json",
            ".venv/bin/python hooks/install.py",
            f"existing top-level events: {sorted(sections.keys())}",
        )

    expected = {"stop", "subagentStop", "sessionEnd", "postToolUse", "afterAgentResponse"}
    have = set(aria_events)
    missing = expected - have
    if missing:
        return (
            False,
            f"aria hooks installed but missing events: {sorted(missing)}",
            ".venv/bin/python hooks/install.py",
            f"have: {sorted(have)}",
        )

    return True, "", "", f"{aria_count} aria hook entries across {sorted(have)}"


async def probe_applescript_cursor() -> tuple[bool, str, str, str]:
    """AppleScript can query the Cursor process (needed for remote-control tools).

    Does NOT require Cursor to be running — only that the AppleScript /
    System Events query succeeds. If Cursor isn't running, the query
    returns empty, which is still a successful probe (Aria will note
    "no Cursor windows open" at tool call time).
    """
    script = (
        'tell application "System Events"\n'
        '  if exists process "Cursor" then\n'
        '    return ((count of (every window of process "Cursor")) as text)\n'
        '  else\n'
        '    return "NOTRUNNING"\n'
        '  end if\n'
        'end tell'
    )
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=4.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, "AppleScript Cursor query timed out after 4s", "", ""
    if proc.returncode != 0:
        err = stderr.decode().strip()
        if "-1743" in err or "not allowed assistive access" in err.lower():
            return (
                False,
                "Accessibility denied for AppleScript -> System Events -> Cursor",
                "System Settings → Privacy & Security → Accessibility → grant your terminal/launcher",
                err[:200],
            )
        return False, f"osascript failed: {err[:200]}", "", ""
    out = stdout.decode().strip()
    if out == "NOTRUNNING":
        return True, "", "", "Cursor not currently running (probe still OK)"
    return True, "", "", f"Cursor windows visible: {out}"


async def probe_prompts() -> tuple[bool, str, str, str]:
    """All prompt templates load without falling back."""
    from .prompts import load_template

    templates = [
        "gemini_system",
        "do_with_claude_system",
        "planning",
        "implementation",
        "architecture",
        "bug-analysis",
        "refactor",
        "cc_plan",
        "cc_implement",
        "cc_verify",
        "cc_merge_upstream",
        "cc_chat_review",
    ]
    sizes = {}
    for name in templates:
        text = load_template(name)
        if len(text) < 50:
            return False, f"template '{name}' suspiciously short ({len(text)} chars)", "", ""
        sizes[name] = len(text)
    return True, "", "", json.dumps(sizes)


async def probe_models_yaml() -> tuple[bool, str, str, str]:
    """models.yaml exists, parses, and all referenced api_key_env vars are set."""
    from .config import config
    import yaml

    path = config.models_config
    if not os.path.exists(path):
        return False, f"models.yaml not found at {path}", "Create models.yaml in project root", ""

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        return False, f"models.yaml parse error: {exc}", "Fix YAML syntax in models.yaml", ""

    if not isinstance(data, dict) or "models" not in data:
        return False, "models.yaml missing top-level 'models' key", "", ""

    models = data["models"]
    missing_keys: list[str] = []
    for name, spec in models.items():
        if not isinstance(spec, dict):
            continue
        env_var = spec.get("api_key_env", "")
        if env_var and not os.getenv(env_var):
            if spec.get("note") and "not available" in spec["note"].lower():
                continue
            missing_keys.append(f"{name}: ${env_var}")

    if missing_keys:
        return (
            False,
            f"API keys not set for: {', '.join(missing_keys)}",
            "Set missing keys in .env",
            f"{len(models)} models, {len(missing_keys)} missing keys",
        )

    return True, "", "", f"{len(models)} models, all keys present"


async def probe_loop_tables() -> tuple[bool, str, str, str]:
    """prompt_versions and loop_executions tables exist and accept writes."""
    from .db import get_connection

    try:
        with get_connection() as conn:
            conn.execute("SELECT COUNT(*) FROM prompt_versions")
            conn.execute("SELECT COUNT(*) FROM loop_executions")
    except Exception as exc:
        return False, f"UCS tables missing or broken: {exc}", "Restart bot to re-run init_db()", ""

    return True, "", "", "prompt_versions + loop_executions tables OK"


async def probe_judge_available() -> tuple[bool, str, str, str]:
    """Correctness harness: specs exist, tables exist, Gemini Flash reachable."""
    from .db import get_connection
    from .judge import SPECS_DIR, load_spec

    issues: list[str] = []

    if not os.path.isdir(SPECS_DIR):
        issues.append(f"specs directory missing: {SPECS_DIR}")
    else:
        specs_found = [f[:-3] for f in os.listdir(SPECS_DIR) if f.endswith(".md")]
        if not specs_found:
            issues.append("no correctness spec files found in specs/correctness/")

    try:
        with get_connection() as conn:
            conn.execute("SELECT COUNT(*) FROM session_records")
            conn.execute("SELECT COUNT(*) FROM verdicts")
    except Exception as exc:
        issues.append(f"judge tables missing: {exc}")

    if issues:
        return False, "; ".join(issues), "Restart bot to re-run init_db(), check specs/correctness/", ""

    return True, "", "", f"specs: {', '.join(specs_found)}, tables OK"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_preflight_inflight = asyncio.Lock()


async def run_all(
    *,
    mcp_client: Any = None,
    cursor_bridge: Any = None,
    cursor_observer: Any = None,
    alert_callback: Callable | None = None,
    include_gemini: bool = True,
    include_cursor: bool = True,
) -> PreflightReport:
    """Run every probe and return an aggregate report.

    Probes run sequentially (most are cheap; isolating failures matters more
    than parallelism). MCP probes are skipped if mcp_client wasn't passed.
    Serialized by _preflight_inflight to prevent concurrent runs.
    """
    if _preflight_inflight.locked():
        report = PreflightReport(started_at=datetime.now(timezone.utc).isoformat())
        report.results.append(ProbeResult(
            name="preflight_concurrent",
            ok=True,
            severity=INFO,
            detail="Another preflight run is in progress — skipped",
        ))
        report.finished_at = datetime.now(timezone.utc).isoformat()
        return report

    async with _preflight_inflight:
        return await _run_all_inner(
            mcp_client=mcp_client,
            cursor_bridge=cursor_bridge,
            cursor_observer=cursor_observer,
            alert_callback=alert_callback,
            include_gemini=include_gemini,
            include_cursor=include_cursor,
        )


async def probe_ffmpeg() -> tuple[bool, str, str, str]:
    """ffmpeg is on PATH — the voice sidecar needs it to (down/up)sample audio.

    Without it the discord.js bridge's prism-media pipeline fails and Aria can
    neither hear nor speak, with no other visible error. There was no probe for
    this before, so a missing ffmpeg looked like a mysterious 'voice is broken'.
    """
    import shutil
    path = shutil.which("ffmpeg")
    if not path:
        return (
            False,
            "ffmpeg not found on PATH — voice audio (down/upsample) will fail",
            "brew install ffmpeg",
            "",
        )
    return True, "", "", path


async def probe_authorized_voice_user() -> tuple[bool, str, str, str]:
    """AUTHORIZED_VOICE_USER_ID is set and numeric.

    The sidecar drops audio from every other speaker. If this is empty, every
    speaker is forwarded; if it's wrong, Corbin's own audio is silently dropped
    and 'voice conversation doesn't work' with no error surfaced.
    """
    from .config import config
    val = (config.authorized_voice_user_id or "").strip()
    if not val:
        return (
            False,
            "AUTHORIZED_VOICE_USER_ID not set — voice frames are unfiltered",
            "Set AUTHORIZED_VOICE_USER_ID=<your numeric Discord user id> in .env",
            "",
        )
    if not val.isdigit():
        return (
            False,
            f"AUTHORIZED_VOICE_USER_ID is not numeric: {val!r}",
            "Set AUTHORIZED_VOICE_USER_ID to your numeric Discord user id in .env",
            "",
        )
    return True, "", "", f"id={val[:4]}\u2026"


async def probe_messages_fda() -> tuple[bool, str, str, str]:
    """Full Disk Access: the Messages chat.db is readable.

    Reading prior iMessages to personalize a message requires FDA for the host
    process. (Sending also needs Automation permission for Messages, which
    can't be probed non-interactively — noted in the fix hint.)
    """
    db = os.path.expanduser("~/Library/Messages/chat.db")
    if not os.path.exists(db):
        return True, "", "", "no chat.db (Messages not configured) — skipped"
    try:
        with open(db, "rb") as f:
            f.read(16)
    except PermissionError:
        return (
            False,
            "chat.db not readable — Full Disk Access missing; iMessage reads will fail",
            "System Settings > Privacy & Security > Full Disk Access > enable Aria's host (Terminal/Python)",
            "",
        )
    except Exception as exc:
        return False, f"chat.db read error: {exc}", "", ""
    return True, "", "", "chat.db readable"


async def probe_messages_send() -> tuple[bool, str, str, str]:
    """Messages SEND automation: does Python hold the macOS Automation grant
    to control Messages (com.apple.MobileSMS)?

    Reading iMessages uses Full Disk Access (probe_messages_fda). SENDING uses
    AppleScript automation — a SEPARATE TCC permission. Without it a send hangs
    and times out ("Messages did not respond in time") instead of erroring
    cleanly, which is exactly how "text my friend" silently fails. We read the
    user TCC.db directly (no interactive prompt) so this never blocks boot but
    tells the user precisely why outbound texts won't send.
    """
    import sqlite3
    tcc = os.path.expanduser("~/Library/Application Support/com.apple.TCC/TCC.db")
    if not os.path.exists(tcc):
        return True, "", "", "no user TCC.db — skipped"
    fix = (
        "Grant Automation > Messages to the bot's Python: System Settings > "
        "Privacy & Security > Automation. The entry appears after the first send "
        "attempt — approve that one-time prompt (do it while at the Mac). SIP "
        "prevents pre-granting it programmatically."
    )
    try:
        con = sqlite3.connect(f"file:{tcc}?mode=ro&immutable=1", uri=True)
        rows = con.execute(
            "select client, auth_value from access "
            "where service='kTCCServiceAppleEvents' "
            "and indirect_object_identifier='com.apple.MobileSMS'"
        ).fetchall()
        con.close()
    except Exception as exc:
        return True, "", "", f"TCC.db unreadable ({exc}) — cannot verify, skipped"
    py = [(c, a) for (c, a) in rows if "python" in (c or "").lower()]
    if any(a in (2, 3) for _c, a in py):
        return True, "", "", "Messages automation granted"
    if py:
        return False, "Messages send automation DENIED for Python", fix, str(py[:2])
    return (
        False,
        "Messages send automation not yet granted for Python — outbound iMessages will time out",
        fix,
        "",
    )


async def probe_contacts() -> tuple[bool, str, str, str]:
    """Contacts automation: does Python hold the Automation grant to control
    Contacts (com.apple.AddressBook)? Resolving a name -> phone/handle uses
    AppleScript automation (a TCC permission). Checked via TCC.db so we never
    hang ~12s per boot on a pending (and never-answered) permission prompt.
    """
    import sqlite3
    tcc = os.path.expanduser("~/Library/Application Support/com.apple.TCC/TCC.db")
    if not os.path.exists(tcc):
        return True, "", "", "no user TCC.db — skipped"
    fix = (
        "Grant Automation > Contacts to the bot's Python (System Settings > "
        "Privacy & Security > Automation; approve the one-time prompt at the Mac). "
        "Without it, looking a person up by name fails — pass a phone/email handle instead."
    )
    try:
        con = sqlite3.connect(f"file:{tcc}?mode=ro&immutable=1", uri=True)
        rows = con.execute(
            "select client, auth_value from access "
            "where service='kTCCServiceAppleEvents' "
            "and indirect_object_identifier='com.apple.AddressBook'"
        ).fetchall()
        con.close()
    except Exception as exc:
        return True, "", "", f"TCC.db unreadable ({exc}) — skipped"
    py = [(c, a) for (c, a) in rows if "python" in (c or "").lower()]
    if any(a in (2, 3) for _c, a in py):
        return True, "", "", "Contacts automation granted"
    if py:
        return False, "Contacts automation DENIED for Python", fix, str(py[:2])
    return (
        False,
        "Contacts automation not yet granted for Python — name lookups will fail",
        fix,
        "",
    )


async def probe_sparks() -> tuple[bool, str, str, str]:
    """Both DGX Spark nodes answer SSH over Tailscale. Advisory (WARN).

    Aria's spark_* tools (status/verify/setup) reach the two GB10 nodes over
    Tailscale SSH. This is deliberately NON-blocking: a spark being powered off
    must never stop the bot from going ready — it only surfaces here so Aria
    knows the node is down before a user asks. Each node is checked in parallel
    with a short timeout; ssh's own ConnectTimeout governs the unreachable case.
    """
    from . import spark

    async def _one(node: str) -> tuple[str, bool]:
        out, rc = await asyncio.to_thread(spark.ssh_probe, node, "echo ok", timeout=12.0)
        return node, (rc == 0 and "ok" in out)

    outcomes = await asyncio.gather(
        *(_one(n) for n in spark.NODES), return_exceptions=True
    )
    detail_bits: list[str] = []
    down: list[str] = []
    for o in outcomes:
        if isinstance(o, Exception):
            down.append(f"?({type(o).__name__})")
            continue
        node, ok = o
        detail_bits.append(f"{node}={'up' if ok else 'down'}")
        if not ok:
            down.append(node)
    if down:
        return (
            False,
            f"spark node(s) unreachable over Tailscale SSH: {', '.join(down)}",
            "tailscale status | grep spark   # confirm the node is powered on; re-auth Tailscale SSH if prompted",
            ", ".join(detail_bits),
        )
    return True, "", "", ", ".join(detail_bits) or "no spark nodes configured"


async def probe_claude_code() -> tuple[bool, str, str, str]:
    """Claude Code (Agent SDK) does a round-trip on the Max subscription. WARN.

    Strips ANTHROPIC_API_KEY first (the billing guard the driver applies at
    every spawn) so a success proves the spawned `claude` authenticated via the
    subscription OAuth in ~/.claude, NOT the app's per-token key. Advisory: a
    Claude Code hiccup must never block the whole bot.
    """
    from .claude_code import DEFAULT_CLAUDE_CODE_REPO

    if not os.path.isdir(DEFAULT_CLAUDE_CODE_REPO):
        return (
            False,
            f"managed Claude Code repo not found: {DEFAULT_CLAUDE_CODE_REPO}",
            "Duplicate the repo (live_visuals_4_CC) next to ucs2",
            "",
        )
    # Billing guard: force the subscription path for this probe's `claude`.
    os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        from claude_agent_sdk import (
            query, ClaudeAgentOptions, ResultMessage, AssistantMessage, TextBlock,
        )
    except Exception as exc:
        return False, f"claude-agent-sdk import failed: {exc}", ".venv/bin/pip install claude-agent-sdk", ""

    opts = ClaudeAgentOptions(
        cwd=DEFAULT_CLAUDE_CODE_REPO,
        setting_sources=[],          # skip the heavy project CLAUDE.md for a fast health check
        permission_mode="plan",      # no tool execution
        max_turns=1,
        max_budget_usd=1.0,
        system_prompt="You are a health probe. Reply with exactly: PREFLIGHT_OK",
    )
    text = ""
    result: Any = None

    async def _drive() -> None:
        nonlocal text, result
        async for msg in query(prompt="Reply with exactly: PREFLIGHT_OK", options=opts):
            if isinstance(msg, AssistantMessage):
                text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                result = msg

    try:
        await asyncio.wait_for(_drive(), timeout=60)
    except asyncio.TimeoutError:
        return False, "Claude Code round-trip timed out after 60s", "claude /status  (confirm subscription + install)", ""

    if result is None:
        return False, "no ResultMessage from Claude Code round-trip", "claude /login", text[:120]
    if getattr(result, "is_error", False):
        sub = getattr(result, "subtype", "")
        return (
            False,
            f"Claude Code round-trip errored (subtype={sub})",
            "claude /login   (subscription) — and ensure no ANTHROPIC_API_KEY in the launch shell",
            str(getattr(result, "result", ""))[:160],
        )
    cost = getattr(result, "total_cost_usd", None)
    leaked = "ANTHROPIC_API_KEY" in os.environ
    return (
        True, "", "",
        f"subscription round-trip OK (notional cost~${cost}); reply={text[:30]!r}; "
        f"env_key_present_after={leaked}",
    )


async def _run_all_inner(
    *,
    mcp_client: Any = None,
    cursor_bridge: Any = None,
    cursor_observer: Any = None,
    alert_callback: Callable | None = None,
    include_gemini: bool = True,
    include_cursor: bool = True,
) -> PreflightReport:
    """Inner implementation of run_all, called under _preflight_inflight lock."""
    report = PreflightReport(started_at=datetime.now(timezone.utc).isoformat())

    # Probes that don't need outside services
    report.results.append(await _run_probe("config", CRITICAL, probe_config))
    report.results.append(await _run_probe("dep_drift", CRITICAL, probe_dep_drift))
    report.results.append(await _run_probe("deployed_trunk", CRITICAL, probe_deployed_trunk))
    report.results.append(await _run_probe("prompts", CRITICAL, probe_prompts))
    report.results.append(await _run_probe("db", CRITICAL, probe_db))
    report.results.append(await _run_probe("models_yaml", WARN, probe_models_yaml))
    report.results.append(await _run_probe("loop_tables", WARN, probe_loop_tables))
    report.results.append(await _run_probe("judge_available", WARN, probe_judge_available))

    # External APIs
    report.results.append(await _run_probe("anthropic", CRITICAL, probe_anthropic))
    if include_gemini:
        report.results.append(await _run_probe("gemini_connect", CRITICAL, probe_gemini_connect))
        # Connect is necessary but not sufficient: assert the model actually
        # emits audio bytes. WARN (not CRITICAL) so a transient demand-throttle
        # at boot surfaces loudly without bricking the bot.
        report.results.append(await _run_probe("gemini_audio", WARN, probe_gemini_audio))
    report.results.append(await _run_probe("mem0", WARN, probe_mem0))

    # Discord
    if alert_callback is not None:
        report.results.append(
            await _run_probe("discord_post", CRITICAL, lambda: probe_discord_post(alert_callback))
        )

    # MCP — only if a client was provided
    if mcp_client is not None and getattr(mcp_client, "_started", False):
        # A1: CRITICAL — every tool name must match Anthropic's regex
        # before we even enter the loop. Belt-and-suspenders for
        # P2(a) in src/mcp.py.
        report.results.append(
            await _run_probe(
                "mcp_tool_name_regex",
                CRITICAL,
                lambda: probe_mcp_tool_name_regex(mcp_client),
            )
        )
        report.results.append(
            await _run_probe("mcp_filesystem", CRITICAL, lambda: probe_mcp_filesystem(mcp_client))
        )
        report.results.append(
            await _run_probe("mcp_shell", WARN, lambda: probe_mcp_shell(mcp_client))
        )
        report.results.append(
            await _run_probe("mcp_apple_calendar", WARN, lambda: probe_mcp_apple_calendar(mcp_client))
        )
        report.results.append(
            await _run_probe("contacts", WARN, probe_contacts)
        )
        report.results.append(
            await _run_probe("mcp_github", WARN, lambda: probe_mcp_github(mcp_client))
        )
        report.results.append(
            await _run_probe("mcp_google_calendar", WARN, lambda: probe_mcp_gcal(mcp_client))
        )
        report.results.append(
            await _run_probe("mcp_gmail", WARN, lambda: probe_mcp_gmail(mcp_client))
        )
        # A1: smoke test for the time tool. P3 already removes runtime
        # dependence on it; this probe surfaces silent regressions.
        report.results.append(
            await _run_probe("mcp_time", WARN, lambda: probe_mcp_time(mcp_client))
        )

    # Anchor health checks
    report.results.append(
        await _run_probe("anchor_smoke", WARN, probe_anchor_smoke)
    )

    # Cursor bridge
    if include_cursor and cursor_bridge is not None:
        report.results.append(
            await _run_probe("cursor_bridge", WARN, lambda: probe_cursor_bridge(cursor_bridge))
        )

    # Claude Code (Agent SDK) — Aria drives Claude Code on live_visuals_4_CC.
    # Advisory round-trip that also verifies the subscription billing path.
    report.results.append(await _run_probe("claude_code", WARN, probe_claude_code))

    # Voice + messaging environment — silent breakers that previously had no
    # probe, so a missing ffmpeg / wrong voice id / missing Full Disk Access
    # looked like a mysterious "voice/iMessages don't work".
    report.results.append(await _run_probe("ffmpeg", WARN, probe_ffmpeg))
    report.results.append(await _run_probe("authorized_voice_user", WARN, probe_authorized_voice_user))
    report.results.append(await _run_probe("messages_fda", WARN, probe_messages_fda))
    report.results.append(await _run_probe("messages_send", WARN, probe_messages_send))

    # Wake word + accessibility
    report.results.append(await _run_probe("wake_word_model", WARN, probe_wake_word_model))
    report.results.append(await _run_probe("accessibility", WARN, probe_accessibility))

    # External Cursor observer (remote-pilot capability)
    report.results.append(
        await _run_probe("cursor_observer", WARN, lambda: probe_cursor_observer(cursor_observer))
    )
    report.results.append(
        await _run_probe("cursor_hooks_installed", WARN, probe_cursor_hooks_installed)
    )
    report.results.append(
        await _run_probe("applescript_cursor", WARN, probe_applescript_cursor)
    )

    # DGX Spark nodes — advisory reachability. Non-blocking by design: a spark
    # being off is surfaced in #ucs-alerts, never a ready-state blocker.
    report.results.append(await _run_probe("sparks", WARN, probe_sparks))

    report.finished_at = datetime.now(timezone.utc).isoformat()
    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_summary(report: PreflightReport, *, markdown: bool = True) -> str:
    """One-line health summary for healthy / warn-only boots.

    The full per-probe report is a wall of text that dominated #ucs-alerts on
    every launch even when nothing was actually broken ("errors all over the
    place"). On a clean (no-critical) boot we post only this summary; the full
    report still goes to the logs and to the `!preflight` command.
    """
    passed = len(report.passed)
    total = len(report.results)
    warns = report.warnings
    bold = "**" if markdown else ""
    if not warns:
        return f"{bold}Preflight OK{bold} \u2014 {passed}/{total} checks passed."
    names = ", ".join((f"`{w.name}`" if markdown else w.name) for w in warns)
    return (
        f"{bold}Preflight OK{bold} \u2014 {passed}/{total} passed, "
        f"{len(warns)} warning(s): {names}. (full detail in logs)"
    )


def format_report(report: PreflightReport, *, markdown: bool = True) -> str:
    """Format the report for Discord posting (markdown) or CLI (plain)."""
    lines = []
    if report.ok:
        header = "**Preflight: ALL CHECKS PASSED**" if markdown else "Preflight: ALL CHECKS PASSED"
    else:
        crit = len(report.critical_failures)
        header = (
            f"**Preflight: {crit} CRITICAL FAILURE(S) — refusing to enter ready state**"
            if markdown
            else f"Preflight: {crit} CRITICAL FAILURE(S) — refusing to enter ready state"
        )
    lines.append(header)

    for r in report.results:
        if r.ok:
            icon = "OK"
        elif r.severity == CRITICAL:
            icon = "FAIL"
        else:
            icon = "WARN"
        bullet = f"- [{icon}] `{r.name}` ({r.elapsed_ms}ms)"
        if r.detail and r.ok:
            bullet += f" -- {r.detail[:100]}"
        if not r.ok:
            bullet += f" -- {r.error[:200]}"
            if r.fix_command:
                bullet += f"\n  fix: `{r.fix_command}`"
        lines.append(bullet)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


async def _cli_main() -> int:
    """Run preflight outside the bot. Connects MCP and cursor_bridge fresh."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

    mcp_client = None
    cursor_bridge = None

    # Try to bring up MCP for a real probe
    if "--no-mcp" not in sys.argv:
        try:
            from .mcp import init_mcp
            mcp_client = await init_mcp()
        except Exception as exc:
            print(f"WARN: MCP init failed in CLI: {exc}", file=sys.stderr)

    # Try to spawn cursor bridge
    if "--no-cursor" not in sys.argv:
        try:
            from .cursor_bridge import CursorBridge
            cursor_bridge = CursorBridge()
            await cursor_bridge.start()
        except Exception as exc:
            print(f"WARN: Cursor bridge spawn failed in CLI: {exc}", file=sys.stderr)

    report = await run_all(
        mcp_client=mcp_client,
        cursor_bridge=cursor_bridge,
        include_gemini="--no-gemini" not in sys.argv,
        include_cursor=cursor_bridge is not None,
    )

    print(format_report(report, markdown=False))
    print()
    print(f"Total: {len(report.results)} probes | "
          f"passed: {len(report.passed)} | "
          f"critical fail: {len(report.critical_failures)} | "
          f"warnings: {len(report.warnings)}")

    if cursor_bridge is not None:
        try:
            await cursor_bridge.stop()
        except Exception:
            pass
    if mcp_client is not None:
        try:
            await mcp_client.stop_all()
        except Exception:
            pass

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_cli_main()))
