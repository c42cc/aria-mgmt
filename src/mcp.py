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
from .capability import capability_for, contracted_env

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
    "gmail": {
        "command": ["npx", "@gongrzhe/server-gmail-autoauth-mcp"],
        "transport": "stdio",
        "tier_defaults": {"read": "R", "get": "R", "list": "R", "search": "R",
                          "send": "I", "draft": "W", "create": "W", "delete": "I",
                          "modify": "W", "batch": "W"},
    },
}

AUDIT_PATH = os.path.join(config.data_dir, "audit.jsonl")


# A2 — per-server token bucket rate limit. Conservative defaults sized below
# each provider's documented per-user-per-second budget. The agent saw a
# Gmail 429 storm in session 40 because the loop fanned out 9 calls in <20s
# at maxResults:500; throttling pre-dispatch turns that into a typed
# RateLimitError instead of letting it reach the upstream.
_RATE_LIMIT_DEFAULTS: dict[str, float] = {
    "gmail": 3.0,            # Google's docs allow 250 quota/user/s; we stay well below
    "google-calendar": 5.0,
    "apple": 5.0,
    "filesystem": 30.0,
    "shell": 5.0,
    "github": 8.0,
}


class _TokenBucket:
    """Minimal per-server token bucket with capacity == rate (1s window)."""

    __slots__ = ("rate", "capacity", "tokens", "last")

    def __init__(self, rate: float):
        self.rate = max(0.001, rate)
        self.capacity = self.rate
        self.tokens = self.rate
        self.last = time.monotonic()

    def try_acquire(self) -> bool:
        now = time.monotonic()
        # Refill at `rate` tokens/sec up to `capacity`.
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


_buckets: dict[str, _TokenBucket] = {}


def _bucket_for(server: str) -> _TokenBucket:
    if server not in _buckets:
        _buckets[server] = _TokenBucket(_RATE_LIMIT_DEFAULTS.get(server, 10.0))
    return _buckets[server]

_ENV_VAR_PATTERN = re.compile(r"[A-Z_]{4,}=\S+")
_VALID_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _sanitize_tool_name(name: str) -> str:
    """Replace characters invalid for Anthropic's tool name pattern with underscores."""
    if _VALID_TOOL_NAME.match(name):
        return name
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:128]
    log.info("Sanitized MCP tool name: %r -> %r", name, sanitized)
    return sanitized


# P2 — MCP boundary validation. Typed error classes Aria sees instead of
# raw server strings. The prompt teaches Aria how to handle each class.
ERR_PERMISSION = "permission"
ERR_RATE_LIMIT = "rate_limit"
ERR_TRANSIENT = "transient"
ERR_DECLINED = "declined"
ERR_SCHEMA = "schema"
ERR_UNKNOWN = "unknown"

# Substring patterns (lowercased) per error class. Order matters — we test
# permission first because "permission denied" must beat the broader
# "denied"/"declined" match in the declined bucket.
_ERROR_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    (ERR_PERMISSION, (
        "full disk access",
        "calendar permission",
        "permission is write-only",
        "not authorized",
        "tcc",
        "operation not permitted",
        "fda",
        "access denied",
        "permission denied",
        "grant full disk",
    )),
    (ERR_RATE_LIMIT, (
        "quota exceeded",
        "rate limit",
        "too many requests",
        "ratelimitexceeded",
        "retry-after",
        " 429 ",
        "rate_limit_exceeded",
    )),
    (ERR_TRANSIENT, (
        "did not respond in time",
        "messages did not respond",
        "notes did not respond",
        "connection reset",
        "connection refused",
        "temporarily unavailable",
        "econnreset",
        "etimedout",
    )),
    (ERR_SCHEMA, (
        "unknown action",
        "unknown command",
        "invalid action",
        "invalid argument",
        "missing required",
        "expected type",
        "schema validation",
        "required parameter",
        "must be one of",
        "is not a valid",
    )),
]


# Server-side schema errors sometimes have a verb in the middle, e.g.
# "Unknown mail_messages action: list". Regex catches the
# "<verb> <noun> action" shape that mcp-macos emits.
_SCHEMA_REGEX = re.compile(
    r"(unknown|invalid|unsupported)\s+\w+\s+(action|command|mode)\b",
    re.IGNORECASE,
)


# macOS app-scripting hangs are a PERMISSION wall, not a transient blip.
# When the Apple MCP drives Messages/Contacts/Notes/Calendar via AppleScript and
# the TCC Automation grant for the controlling process is missing, the AppleEvent
# never gets a response and the call HANGS, then times out with one of these
# strings (or returns a -1743 "not allowed to send Apple events"). Classifying
# that as ERR_TRANSIENT is exactly what made Aria thrash through ~10 doomed send
# workarounds instead of saying "grant Messages Automation" once and stopping.
_APPLE_AUTOMATION_HANG_PATTERNS: tuple[str, ...] = (
    "did not respond in time",
    "messages did not respond",
    "contacts did not respond",
    "notes did not respond",
    "system events did not respond",
    "apple event timed out",
    "appleevent timed out",
    "isn't running",
    "isn\u2019t running",
    "is not running",
    "not allowed to send apple events",
    "not authorized to send apple events",
    "erraeeventnotpermitted",
    "-1743",
)
_APPLE_APP_BY_KEYWORD: tuple[tuple[str, str], ...] = (
    ("imessage", "Messages"), ("message", "Messages"), ("contact", "Contacts"),
    ("note", "Notes"), ("reminder", "Reminders"), ("calendar", "Calendar"),
    ("event", "Calendar"),
)


def _is_apple_app_scripting_tool(server_name: str, tool_name: str) -> bool:
    """True for the Apple MCP (Messages/Contacts/Notes/Calendar via AppleScript).

    Scoped to the `apple` server so a Gmail/Google timeout that happens to say
    'did not respond in time' is NOT misread as a macOS Automation wall.
    """
    if (server_name or "").lower() == "apple":
        return True
    blob = f"{server_name} {tool_name}".lower()
    return any(k in blob for k in ("imessage", "messages_", "contacts", "notes_", "reminder"))


def _apple_app_name(server_name: str, tool_name: str) -> str:
    blob = f"{server_name} {tool_name}".lower()
    for kw, app in _APPLE_APP_BY_KEYWORD:
        if kw in blob:
            return app
    return "the Apple app"


def _classify_error_text(text: str, tool_name: str = "", server_name: str = "") -> str | None:
    """Return an ERR_* class for known error substrings, or None if unrecognised.

    Tool-aware: an Apple app-scripting hang/timeout is reclassified PERMISSION
    (it is a missing TCC Automation grant), pre-empting the transient bucket.
    """
    if not text:
        return None
    lower = text.lower()[:4000]
    if _is_apple_app_scripting_tool(server_name, tool_name) and any(
        p in lower for p in _APPLE_AUTOMATION_HANG_PATTERNS
    ):
        return ERR_PERMISSION
    for cls, patterns in _ERROR_PATTERNS:
        if any(p in lower for p in patterns):
            return cls
    if _SCHEMA_REGEX.search(text[:4000]):
        return ERR_SCHEMA
    return None


def _permission_message(tool_name: str, server_name: str, err_class: str) -> str:
    """The exact, actionable message for a recognized error — with the macOS
    Automation grant-fix when it's an Apple app-scripting permission wall."""
    if err_class == ERR_PERMISSION and _is_apple_app_scripting_tool(server_name, tool_name):
        app = _apple_app_name(server_name, tool_name)
        return (
            f"macOS is blocking automation of {app}: the AppleScript call hung and "
            f"timed out because the bot's python lacks the Automation grant. "
            f"ONE-COMMAND FIX (owner, at the Mac): run "
            f"`.venv/bin/python scripts/provision_imessage.py` — it flips the stuck "
            f"grant and verifies green. (Or System Settings > Privacy & Security > "
            f"Automation > enable {app}.) Do NOT retry or improvise another send path."
        )
    return f"Tool '{tool_name}' returned a {err_class} error."


def _typed_error(cls: str, message: str, raw: str) -> str:
    """Wrap a recognized tool error in the typed JSON envelope Aria's prompt understands."""
    hints = {
        ERR_PERMISSION: (
            "Tell the user the exact permission that is missing and how to grant it "
            "(macOS path or OAuth scope). Do not retry the same tool."
        ),
        ERR_RATE_LIMIT: (
            "Back off. Do not retry the same call within this turn. Use an alternate "
            "data source if one is available, or summarise what you already have and stop."
        ),
        ERR_TRANSIENT: (
            "Retry at most once. If it fails again, surface the issue to the user — "
            "do not fabricate a result."
        ),
        ERR_DECLINED: (
            "The user did not approve this action. Ask the user whether to retry, "
            "or pick a different approach. Do not silently re-issue the same call."
        ),
        ERR_SCHEMA: (
            "Re-read the tool's input_schema before retrying. Do not guess argument "
            "values. If uncertain, ask the user."
        ),
        ERR_UNKNOWN: (
            "Report the failure to the user; do not invent a result."
        ),
    }
    return json.dumps({
        "_error_class": cls,
        "_message": message,
        "_hint": hints.get(cls, hints[ERR_UNKNOWN]),
        "_raw": raw[:3000],
    })


def _extract_tool_text(result: Any) -> str:
    """Extract the model/user-facing text from an MCP CallToolResult.

    This is the single extraction chokepoint for every MCP-backed action.
    The SDK returns `result.content` as a list of typed content blocks
    (TextContent, ImageContent, AudioContent, EmbeddedResource,
    ResourceLink). The payload Aria needs is each text block's `.text`.

    The prior `str(result.content)` emitted the Python *repr* of that list —
    `[TextContent(type='text', text='...', annotations=None, meta=None)]` —
    leaking the `annotations=None, meta=None` nulls and the `TextContent(...)`
    wrapper into the audit log, the agent loop's view of every result, and
    (via quick_email_check / quick_calendar) straight into Aria's voice
    readout. One repr at the root polluted every channel.

    Non-text blocks become explicit, compact markers so nothing is silently
    dropped. An unrecognised block type is logged loudly (not swallowed) and
    rendered as a typed marker rather than repr-dumped — there is no silent
    fallback to `str()`.
    """
    content = getattr(result, "content", None)
    if content is None:
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return json.dumps(structured, default=str)
        raise RuntimeError(
            "MCP CallToolResult has neither content nor structuredContent — "
            "cannot extract a tool result"
        )

    parts: list[str] = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(block.text)
        elif btype == "image":
            parts.append(f"[image: {getattr(block, 'mimeType', 'image')}]")
        elif btype == "audio":
            parts.append(f"[audio: {getattr(block, 'mimeType', 'audio')}]")
        elif btype == "resource_link":
            parts.append(f"[resource_link: {getattr(block, 'uri', '')}]")
        elif btype == "resource":
            res = getattr(block, "resource", None)
            text = getattr(res, "text", None)
            if text is not None:
                parts.append(text)
            else:
                marker = f"[resource: {getattr(res, 'uri', '')} {getattr(res, 'mimeType', '')}]"
                parts.append(marker.strip())
        else:
            log.warning("MCP returned unrecognised content block type=%r", btype)
            parts.append(f"[unsupported content block: type={btype}]")

    if not parts:
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return json.dumps(structured, default=str)
    return "\n".join(parts)


def _validate_args_against_schema(args: dict, schema: dict) -> str | None:
    """Light JSON-Schema check for the dimensions Aria most often gets wrong.

    Returns None on pass, or a human-readable error string on failure.
    Intentionally conservative — we only fail on issues the schema declares
    explicitly. Unknown schema features (e.g. `oneOf`, `allOf`) are skipped
    so we never reject calls the server would actually accept.
    """
    if not isinstance(schema, dict):
        return None
    if schema.get("type") not in (None, "object"):
        return None  # only object schemas validated here

    props = schema.get("properties") or {}
    required = schema.get("required") or []

    for key in required:
        if key not in args:
            return f"missing required argument '{key}'"

    for key, value in args.items():
        spec = props.get(key)
        if not isinstance(spec, dict):
            continue
        enum = spec.get("enum")
        if enum is not None and value not in enum:
            return (
                f"argument '{key}'={value!r} not in allowed values "
                f"{enum!r}"
            )
        expected_type = spec.get("type")
        if expected_type:
            ok = _matches_jsonschema_type(value, expected_type)
            if not ok:
                return (
                    f"argument '{key}' expected type {expected_type!r}, "
                    f"got {type(value).__name__}={value!r:.80s}"
                )
    return None


def _matches_jsonschema_type(value: Any, expected: Any) -> bool:
    """Best-effort JSON-Schema type check. Accepts list-of-types per spec."""
    if isinstance(expected, list):
        return any(_matches_jsonschema_type(value, t) for t in expected)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True  # unknown types pass


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
        "result_summary": result_summary[:1_000_000],
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
        self._original_names: dict[str, str] = {}
        self._started = False
        self._confirm_callback: Any = None

    def set_confirm_callback(self, cb: Any) -> None:
        self._confirm_callback = cb

    async def start_all(self) -> None:
        """Start all configured MCP servers and collect their tool catalogs. Idempotent."""
        if self._started:
            return
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def _start_one(name: str, cfg: dict) -> None:
            cmd = cfg["command"]
            # One contracted environment: PATH always carries /usr/sbin
            # (screencapture) and Homebrew bins, so an OS action never fails
            # with "command not found" because of how the bot was launched.
            env = contracted_env(extra=cfg.get("env", {}))
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
                safe_name = _sanitize_tool_name(tool.name)
                self._tools[safe_name] = {
                    "name": safe_name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                    "server": name,
                }
                self._tool_to_server[safe_name] = name
                if safe_name != tool.name:
                    self._original_names[safe_name] = tool.name

            self._servers[name] = session

            if name == "apple":
                for tname in list(self._tools.keys()):
                    if tname.startswith("mail_") and self._tool_to_server.get(tname) == "apple":
                        del self._tools[tname]
                        del self._tool_to_server[tname]
                        self._original_names.pop(tname, None)
                        log.info("Filtered apple mail tool: %s (Gmail-only policy)", tname)

            log.info("MCP server '%s' started: %d tools", name, len(
                [t for t in self._tools if self._tool_to_server.get(t) == name]))

        for name, cfg in MCP_SERVERS.items():
            try:
                await _start_one(name, cfg)
            except Exception as e:
                log.error("Failed to start MCP server '%s': %s", name, e)

        self._started = True
        log.info("MCP fleet ready: %d/%d servers", len(self._servers), len(MCP_SERVERS))

    def list_tools_anthropic(self) -> list[dict]:
        """Return tools in Anthropic API format.

        P2(a): every emitted name must match Anthropic's regex
        `^[a-zA-Z0-9_-]{1,128}$`. `_sanitize_tool_name` already runs at
        registration; this is a belt-and-suspenders check that fails loud
        if a non-conforming name ever slips through (e.g. via a future code
        path that bypasses sanitization).
        """
        result = []
        for t in self._tools.values():
            name = t["name"]
            if not _VALID_TOOL_NAME.match(name):
                log.error(
                    "MCP boundary violation: tool name %r does not match Anthropic regex; "
                    "re-sanitizing on the fly. Fix the registration path.",
                    name,
                )
                name = _sanitize_tool_name(name)
            result.append({
                "name": name,
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

        # Capability precheck — confirm we CAN do this before we try. An unmet
        # precondition (e.g. the Messages Automation grant) returns the exact
        # one-command fix and the action NEVER fires: no hang, no brute-force
        # thrash, no blaming the user for a wall we could see coming. This is
        # the mechanical enforcement that replaces the prompt's "don't
        # improvise another send path" prose.
        unmet = capability_for(server_name, tool_name, args).unmet()
        if unmet:
            typed = _typed_error(ERR_PERMISSION, unmet, "capability precondition unmet")
            _audit_log(
                server_name, tool_name, args, typed[:1_000_000],
                _classify_tier(server_name, tool_name), None, session_key,
            )
            log.info("capability precheck blocked %s.%s", server_name, tool_name)
            return typed

        # A2 — per-server rate limit. Aria sees the same typed RateLimitError
        # she would see from an upstream 429, but earlier and without
        # consuming the provider's quota.
        if not _bucket_for(server_name).try_acquire():
            log.warning(
                "MCP rate-limit pre-empt: server=%s tool=%s",
                server_name, tool_name,
            )
            return _typed_error(
                ERR_RATE_LIMIT,
                f"Server '{server_name}' rate-limited locally (tool={tool_name}).",
                f"local token bucket exhausted for {server_name}",
            )

        # P2(b) — args-vs-schema validation. Reject malformed calls before
        # they hit the server so Aria sees a typed schema error with the
        # actual constraint, rather than the server's terse "Unknown action".
        tool_spec = self._tools.get(tool_name) or {}
        input_schema = tool_spec.get("input_schema") or {}
        schema_err = _validate_args_against_schema(args, input_schema)
        if schema_err:
            log.warning(
                "MCP arg-schema violation: tool=%s err=%s args=%s",
                tool_name, schema_err, str(args)[:200],
            )
            typed = _typed_error(
                ERR_SCHEMA,
                f"Tool '{tool_name}' rejected the arguments before dispatch: {schema_err}",
                schema_err,
            )
            _audit_log(server_name, tool_name, args, typed[:1_000_000], _classify_tier(server_name, tool_name), None, session_key)
            return typed

        tier = _classify_tier(server_name, tool_name)
        confirmed = None

        # Per-command confirmation is OFF by default (Corbin's call): tier-I/X
        # tools run autonomously and the audit log records them. Human approval
        # lives at the approach level via propose_action, not per command. Set
        # CONFIRM_RISKY_TOOLS=true to restore the per-command gate.
        if tier in ("I", "X") and config.confirm_risky_tools and self._confirm_callback:
            import uuid
            action_id = str(uuid.uuid4())[:8]
            summary = f"{tool_name}({json.dumps(args)[:200]})"
            result = await self._confirm_callback(action_id, tool_name, summary)
            if not result.get("approved", False):
                _audit_log(server_name, tool_name, args, "declined", tier, False, session_key)
                if result.get("timeout"):
                    return _typed_error(
                        ERR_DECLINED,
                        "Tier-X/I confirmation timed out — the user did not respond.",
                        "confirmation timed out",
                    )
                mods = result.get("modifications")
                if mods:
                    return _typed_error(
                        ERR_DECLINED,
                        f"User requested changes before approving: {mods}",
                        f"user requested changes: {mods}",
                    )
                return _typed_error(
                    ERR_DECLINED,
                    "The user declined this action.",
                    "user declined",
                )
            confirmed = True
        elif tier in ("I", "X"):
            log.info(
                "tier-%s tool '%s' executing autonomously (per-command confirm off; audited)",
                tier, tool_name,
            )

        try:
            wire_name = self._original_names.get(tool_name, tool_name)
            result = await session.call_tool(wire_name, args)
            result_text = _extract_tool_text(result)
            _audit_log(server_name, tool_name, args, result_text[:1_000_000], tier, confirmed, session_key)

            # P2(c) — typed error classification on the success path. Some
            # MCP servers return errors as ordinary content (Apple Mail's
            # "Cannot access Mail database…", Gmail's "Quota exceeded…").
            # If the text matches a known error class, wrap it so Aria's
            # prompt rules apply.
            err_class = _classify_error_text(result_text, tool_name=tool_name, server_name=server_name)
            if err_class is not None:
                return _typed_error(err_class, _permission_message(tool_name, server_name, err_class), result_text)

            return result_text[:50_000]
        except Exception as e:
            error_msg = f"MCP tool error: {e}"
            _audit_log(server_name, tool_name, args, error_msg, tier, confirmed, session_key)
            err_class = _classify_error_text(str(e), tool_name=tool_name, server_name=server_name) or ERR_UNKNOWN
            return _typed_error(err_class, _permission_message(tool_name, server_name, err_class), error_msg)

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
    """Initialize and start the global MCP client. Idempotent."""
    global mcp_client
    if mcp_client is not None and mcp_client._started:
        return mcp_client
    mcp_client = MCPClient()
    await mcp_client.start_all()
    return mcp_client
