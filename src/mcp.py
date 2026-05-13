"""MCP client manager — spawns servers, collects tools, dispatches calls, audits."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from .config import config

log = logging.getLogger(__name__)

MCP_SERVERS: dict[str, dict[str, Any]] = {
    "apple": {
        "command": ["npx", "mcp-macos"],
        "transport": "stdio",
        "tier_defaults": {"read": "R", "get": "R", "list": "R", "search": "R",
                          "create": "W", "send": "I", "delete": "I"},
    },
    "google-calendar": {
        "command": ["npx", "@cocal/google-calendar-mcp"],
        "transport": "stdio",
        "env": {
            "GOOGLE_OAUTH_CREDENTIALS": os.getenv(
                "GOOGLE_OAUTH_CREDENTIALS",
                os.path.expanduser("~/.config/google-calendar-mcp/gcp-oauth.keys.json"),
            ),
            "GOOGLE_CALENDAR_MCP_TOKEN_PATH": os.getenv(
                "GOOGLE_CALENDAR_MCP_TOKEN_PATH",
                os.path.expanduser("~/.config/google-calendar-mcp/tokens.json"),
            ),
        },
        "tier_defaults": {"list": "R", "get": "R", "create": "W", "update": "W", "delete": "I"},
    },
    "filesystem": {
        "command": ["npx", "@modelcontextprotocol/server-filesystem",
                     "/Users/corbin/Documents", "/Users/corbin/Downloads",
                     "/Users/corbin/PycharmProjects"],
        "transport": "stdio",
        "tier_defaults": {"read": "R", "write": "W", "delete": "I", "move": "W",
                          "search": "R", "list": "R", "get": "R"},
    },
    "shell": {
        "command": ["npx", "mcp-shell-execute"],
        "transport": "stdio",
        "tier_defaults": {"execute": "X", "run": "X", "shell": "X"},
    },
    "github": {
        "command": ["npx", "@modelcontextprotocol/server-github"],
        "transport": "stdio",
        "env": {"GITHUB_TOKEN": os.getenv("GITHUB_TOKEN", os.getenv("GITHUB_TOKEN_MORE_SCOPE", ""))},
        "tier_defaults": {"get": "R", "list": "R", "create": "W", "update": "W", "search": "R"},
    },
}

AUDIT_PATH = os.path.join(config.data_dir, "audit.jsonl")

_ENV_VAR_PATTERN = re.compile(r"[A-Z_]{4,}=\S+")


def _redact_args(tool_name: str, args: dict) -> dict:
    """Apply per-tool redaction for audit logging."""
    redacted = {}
    for k, v in args.items():
        sv = str(v)
        if any(kw in tool_name.lower() for kw in ("send", "draft", "email", "message")):
            if k in ("body", "content", "text", "message") and len(sv) > 200:
                redacted[k] = sv[:200] + f"... [{len(sv)} chars total]"
                continue
        if any(kw in tool_name.lower() for kw in ("shell", "execute", "run")):
            sv = _ENV_VAR_PATTERN.sub("[REDACTED]", sv)
        if any(kw in tool_name.lower() for kw in ("write_file", "create_file")):
            if k in ("content", "data", "body"):
                redacted[k] = f"[{len(sv)} chars, redacted]"
                continue
        redacted[k] = sv if len(sv) <= 500 else sv[:500] + "..."
    return redacted


def _classify_tier(server_name: str, tool_name: str) -> str:
    """Classify a tool's risk tier from server config."""
    server_cfg = MCP_SERVERS.get(server_name, {})
    defaults = server_cfg.get("tier_defaults", {})
    for prefix, tier in defaults.items():
        if tool_name.lower().startswith(prefix):
            return tier
    return "W"


def _audit_log(
    server: str, tool: str, args: dict, result_summary: str,
    tier: str, confirmed: bool | None, session_key: str = "",
) -> None:
    """Append one line to data/audit.jsonl."""
    os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "server": server,
        "tool": tool,
        "args": _redact_args(tool, args),
        "result_summary": result_summary[:500],
        "tier": tier,
        "confirmed": confirmed,
        "session_key": session_key,
    }
    with open(AUDIT_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


class MCPClient:
    """Manages MCP server subprocesses and tool dispatch."""

    def __init__(self):
        self._servers: dict[str, Any] = {}
        self._tools: dict[str, dict] = {}
        self._tool_to_server: dict[str, str] = {}
        self._started = False
        self._confirm_callback: Any = None

    def set_confirm_callback(self, cb: Any) -> None:
        self._confirm_callback = cb

    async def start_all(self) -> None:
        """Start all configured MCP servers and collect their tool catalogs."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def _start_one(name: str, cfg: dict) -> None:
            cmd = cfg["command"]
            env = {**os.environ, **cfg.get("env", {})}
            params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)

            log.info("MCP: starting '%s' (%s)...", name, " ".join(cmd[:2]))

            global _exit_stack
            if _exit_stack is None:
                _exit_stack = AsyncExitStack()

            streams = await asyncio.wait_for(
                _exit_stack.enter_async_context(stdio_client(params)), timeout=15,
            )
            read_stream, write_stream = streams
            session = await asyncio.wait_for(
                _exit_stack.enter_async_context(ClientSession(read_stream, write_stream)), timeout=15,
            )
            await asyncio.wait_for(session.initialize(), timeout=15)
            tools_result = await asyncio.wait_for(session.list_tools(), timeout=10)

            for tool in tools_result.tools:
                self._tools[tool.name] = {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                    "server": name,
                }
                self._tool_to_server[tool.name] = name

            self._servers[name] = session
            log.info("MCP server '%s' started: %d tools", name, len(tools_result.tools))

        for name, cfg in MCP_SERVERS.items():
            try:
                await _start_one(name, cfg)
            except Exception as e:
                log.error("Failed to start MCP server '%s': %s", name, e)

        self._started = True
        log.info("MCP fleet ready: %d/%d servers", len(self._servers), len(MCP_SERVERS))

    def list_tools_anthropic(self) -> list[dict]:
        """Return tools in Anthropic API format."""
        result = []
        for t in self._tools.values():
            result.append({
                "name": t["name"],
                "description": t["description"],
                "input_schema": t.get("input_schema", {"type": "object", "properties": {}}),
            })
        return result

    async def call_tool(
        self, tool_name: str, args: dict, session_key: str = ""
    ) -> str:
        """Dispatch a tool call to the appropriate MCP server. Includes tier check and audit."""
        server_name = self._tool_to_server.get(tool_name)
        if not server_name:
            return json.dumps({"error": f"Unknown MCP tool: {tool_name}"})

        session = self._servers.get(server_name)
        if not session:
            return json.dumps({"error": f"MCP server '{server_name}' not available"})

        tier = _classify_tier(server_name, tool_name)
        confirmed = None

        if tier in ("I", "X") and self._confirm_callback:
            import uuid
            action_id = str(uuid.uuid4())[:8]
            summary = f"{tool_name}({json.dumps(args)[:200]})"
            result = await self._confirm_callback(action_id, tool_name, summary)
            if not result.get("approved", False):
                _audit_log(server_name, tool_name, args, "declined", tier, False, session_key)
                if result.get("timeout"):
                    return json.dumps({"declined": True, "reason": "confirmation timed out"})
                mods = result.get("modifications")
                if mods:
                    return json.dumps({"declined": True, "reason": f"user requested changes: {mods}"})
                return json.dumps({"declined": True, "reason": "user declined"})
            confirmed = True

        try:
            result = await session.call_tool(tool_name, args)
            result_text = str(result.content) if hasattr(result, "content") else str(result)
            _audit_log(server_name, tool_name, args, result_text[:500], tier, confirmed, session_key)
            return result_text[:4000]
        except Exception as e:
            error_msg = f"MCP tool error: {e}"
            _audit_log(server_name, tool_name, args, error_msg, tier, confirmed, session_key)
            return json.dumps({"error": error_msg})

    async def health_check(self) -> str:
        """Return per-server health status."""
        parts = []
        for name in MCP_SERVERS:
            if name in self._servers:
                parts.append(f"{name}: ok")
            else:
                parts.append(f"{name}: down")
        return ", ".join(parts) if parts else "no servers configured"

    async def stop_all(self) -> None:
        """Shut down all MCP server sessions."""
        global _exit_stack
        for name, session in self._servers.items():
            try:
                await session.close()
            except Exception:
                log.warning("Error closing MCP server '%s'", name)
        self._servers.clear()
        self._started = False
        if _exit_stack is not None:
            await _exit_stack.aclose()
            _exit_stack = None


import asyncio
from contextlib import AsyncExitStack


_exit_stack: AsyncExitStack | None = None


async def _open_stdio(params):
    """Open a stdio connection to an MCP server."""
    global _exit_stack
    if _exit_stack is None:
        _exit_stack = AsyncExitStack()
    from mcp.client.stdio import stdio_client
    return await _exit_stack.enter_async_context(stdio_client(params))


mcp_client: MCPClient | None = None


async def init_mcp() -> MCPClient:
    """Initialize and start the global MCP client."""
    global mcp_client
    mcp_client = MCPClient()
    await mcp_client.start_all()
    return mcp_client
