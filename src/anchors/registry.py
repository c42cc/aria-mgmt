"""Tool name -> Anchor class mapping.

Includes a short-TTL cache + concurrent-call coalesce so two parallel agent
loops triggering the judge on the same `(tool, args)` hit the upstream API
once, not twice (L6 fix from the audit). The cache key includes a coarse
time bucket so anchor results stay current to within `_CACHE_TTL_SEC`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from .base import Anchor, AnchorReport
from .calendar_google import GoogleCalendarAnchor
from .claimed_delivery import ClaimedDeliveryAnchor
from .filesystem import (
    FilesystemListAnchor,
    FilesystemReadAnchor,
    FilesystemSearchAnchor,
    FilesystemWriteAnchor,
)
from .github_anchor import GithubAnchor
from .gmail import GmailReadAnchor, GmailSearchAnchor, GmailSendAnchor
from .plan_citation import PlanCitationAnchor
from .shell_anchor import ShellAnchor

log = logging.getLogger(__name__)

_REGISTRY: dict[str, Anchor] = {}

_TOOL_MAP: dict[str, type] = {
    "search_emails": GmailSearchAnchor,
    "read_email": GmailReadAnchor,
    "get_email": GmailReadAnchor,
    "send_email": GmailSendAnchor,

    "list-events": GoogleCalendarAnchor,
    "list_events": GoogleCalendarAnchor,
    "get-event": GoogleCalendarAnchor,
    "get_event": GoogleCalendarAnchor,
    "create-event": GoogleCalendarAnchor,
    "create_event": GoogleCalendarAnchor,
    "list-calendars": GoogleCalendarAnchor,
    "list_calendars": GoogleCalendarAnchor,

    "read_file": FilesystemReadAnchor,
    "list_directory": FilesystemListAnchor,
    "list_allowed_directories": FilesystemListAnchor,
    "search_files": FilesystemSearchAnchor,
    "write_file": FilesystemWriteAnchor,

    "list_commits": GithubAnchor,
    "list_issues": GithubAnchor,
    "list_pulls": GithubAnchor,
    "search_repositories": GithubAnchor,
    "get_pull_request": GithubAnchor,

    "execute_command": ShellAnchor,
    "execute-command": ShellAnchor,

    "plan_with_claude": PlanCitationAnchor,

    # DP4: a cursor_send that claims delivery it never verified is failed
    # deterministically — the judge's narration-trust is no longer the only gate.
    "cursor_send": ClaimedDeliveryAnchor,
}


_CACHE_TTL_SEC = 60.0
# Write/irreversible anchors must not be cached — every call is a side-effect
# verification of a unique intent. Only READ anchors get pooled.
_WRITE_TOOLS = frozenset({
    "send_email",
    "create-event", "create_event",
    "write_file",
    "execute_command", "execute-command",
    "cursor_send",
})


_cache: dict[str, tuple[float, AnchorReport]] = {}
_inflight: dict[str, asyncio.Future] = {}


def anchor_for(tool_name: str) -> Anchor | None:
    """Get or create an anchor instance for a tool name. Returns None if no anchor exists."""
    if tool_name not in _TOOL_MAP:
        return None
    if tool_name not in _REGISTRY:
        _REGISTRY[tool_name] = _TOOL_MAP[tool_name]()
    return _REGISTRY[tool_name]


def _cache_key(tool_name: str, tool_call: dict) -> str:
    """Stable key over the args that drive the upstream request.

    Same shape as `tools._dedup_key` so the audit log and the anchor cache
    use compatible identity for the same call.
    """
    args = tool_call.get("args", {})
    try:
        args_json = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        args_json = repr(args)
    return f"{tool_name}:{args_json}"


async def check_with_cache(
    anchor: Anchor,
    tool_name: str,
    tool_call: dict,
    aria_result: str,
) -> AnchorReport:
    """Run anchor.check() with TTL cache + concurrent-call coalesce.

    Reads cached result if it's <_CACHE_TTL_SEC old. If a sibling task is
    already running the same key, awaits its future instead of firing a
    second upstream request. Write-anchor tools bypass the cache.
    """
    if tool_name in _WRITE_TOOLS:
        return await anchor.check(tool_call, aria_result)

    key = _cache_key(tool_name, tool_call)
    now = time.monotonic()

    cached = _cache.get(key)
    if cached is not None:
        ts, report = cached
        if now - ts < _CACHE_TTL_SEC:
            log.debug("anchor cache hit: %s (age=%.1fs)", tool_name, now - ts)
            return report

    inflight = _inflight.get(key)
    if inflight is not None and not inflight.done():
        log.debug("anchor coalesce: awaiting in-flight %s", tool_name)
        return await inflight

    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _inflight[key] = fut
    try:
        report = await anchor.check(tool_call, aria_result)
        _cache[key] = (time.monotonic(), report)
        fut.set_result(report)
        return report
    except Exception as exc:
        fut.set_exception(exc)
        raise
    finally:
        _inflight.pop(key, None)


def clear_cache() -> None:
    """Drop every cached anchor report. Useful in tests."""
    _cache.clear()
