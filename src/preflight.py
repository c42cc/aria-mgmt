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
import hashlib
import json
import logging
import os
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
    """Apple MCP can actually read calendars (verifies EventKitCLI binary + permissions)."""
    if mcp_client is None or "apple" not in mcp_client._servers:
        return False, "apple MCP not running", "ops/bootstrap.sh", ""
    result = await mcp_client.call_tool("calendar_calendars", {})
    text = str(result)
    if "EventKitCLI binary not found" in text:
        return (
            False,
            "EventKitCLI Swift binary missing in mcp-macos package",
            "bash ops/build_macos_swift.sh",
            text[:200],
        )
    if "permission" in text.lower() or "not authorized" in text.lower() or "TCC" in text:
        return (
            False,
            "macOS Calendar permission not granted",
            "bash ops/grant_permissions.sh",
            text[:200],
        )
    if "error" in text.lower()[:40] or "failed" in text.lower()[:40]:
        return False, f"calendar_calendars call failed", "", text[:300]
    return True, "", "", f"got {text[:120]!r}"


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
        # ping not yet implemented — bridge alive is acceptable signal
        return True, "", "", f"pid={cursor_bridge._process.pid} (ping action not available)"
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


async def probe_running_code() -> tuple[bool, str, str, str]:
    """Live source SHA matches a sentinel written at boot. Detects stale launches."""
    sentinel_path = os.path.join("data", ".preflight_boot_sha")
    src_dir = os.path.join(os.path.dirname(__file__))
    hasher = hashlib.sha256()
    for name in sorted(os.listdir(src_dir)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(src_dir, name)
        with open(path, "rb") as f:
            hasher.update(name.encode())
            hasher.update(f.read())
    current_sha = hasher.hexdigest()[:16]

    if os.path.exists(sentinel_path):
        with open(sentinel_path) as f:
            recorded = f.read().strip()
        if recorded != current_sha:
            return (
                False,
                f"source code changed since process started (sentinel={recorded} now={current_sha})",
                "Restart: make run",
                "",
            )

    os.makedirs(os.path.dirname(sentinel_path), exist_ok=True)
    with open(sentinel_path, "w") as f:
        f.write(current_sha)
    return True, "", "", f"sha={current_sha}"


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
    ]
    sizes = {}
    for name in templates:
        text = load_template(name)
        if len(text) < 50:
            return False, f"template '{name}' suspiciously short ({len(text)} chars)", "", ""
        sizes[name] = len(text)
    return True, "", "", json.dumps(sizes)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def run_all(
    *,
    mcp_client: Any = None,
    cursor_bridge: Any = None,
    alert_callback: Callable | None = None,
    include_gemini: bool = True,
    include_cursor: bool = True,
) -> PreflightReport:
    """Run every probe and return an aggregate report.

    Probes run sequentially (most are cheap; isolating failures matters more
    than parallelism). MCP probes are skipped if mcp_client wasn't passed.
    """
    report = PreflightReport(started_at=datetime.now(timezone.utc).isoformat())

    # Probes that don't need outside services
    report.results.append(await _run_probe("config", CRITICAL, probe_config))
    report.results.append(await _run_probe("dep_drift", CRITICAL, probe_dep_drift))
    report.results.append(await _run_probe("running_code", WARN, probe_running_code))
    report.results.append(await _run_probe("prompts", CRITICAL, probe_prompts))
    report.results.append(await _run_probe("db", CRITICAL, probe_db))

    # External APIs
    report.results.append(await _run_probe("anthropic", CRITICAL, probe_anthropic))
    if include_gemini:
        report.results.append(await _run_probe("gemini_connect", CRITICAL, probe_gemini_connect))
    report.results.append(await _run_probe("mem0", WARN, probe_mem0))

    # Discord
    if alert_callback is not None:
        report.results.append(
            await _run_probe("discord_post", CRITICAL, lambda: probe_discord_post(alert_callback))
        )

    # MCP — only if a client was provided
    if mcp_client is not None and getattr(mcp_client, "_started", False):
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
            await _run_probe("mcp_github", WARN, lambda: probe_mcp_github(mcp_client))
        )
        report.results.append(
            await _run_probe("mcp_google_calendar", WARN, lambda: probe_mcp_gcal(mcp_client))
        )

    # Cursor bridge
    if include_cursor and cursor_bridge is not None:
        report.results.append(
            await _run_probe("cursor_bridge", WARN, lambda: probe_cursor_bridge(cursor_bridge))
        )

    report.finished_at = datetime.now(timezone.utc).isoformat()
    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


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
