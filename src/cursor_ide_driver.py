"""Drive the real Cursor IDE over the Chrome DevTools Protocol — the actuator
that replaces osascript "paste-and-pray".

The forensic this kills (UCS thread, 2026-06-19 06:18 UTC): Aria pasted a
message into a Cursor window via osascript, the paste "timed out on mtime
confirmation", and the result was narrated as *"Sent. The message was
delivered… it'll pick it up when the Cursor agent resumes."* Nothing had
landed, and no autonomous agent existed to pick it up. The lie had two halves:
the send itself was **unverified**, and success was **claimed anyway**.

This module fixes both:

  1. It drives the IDE *for real* over CDP — focus the chat composer
     (`Runtime.evaluate`), insert the text as a **trusted** input
     (`Input.insertText`), press Enter as a **trusted** key event
     (`Input.dispatchKeyEvent`). Trusted events fire Cursor's editor +
     keybindings; synthetic DOM events do not.
  2. It claims success **only** when the on-disk Cursor transcript actually
     advances — the genuine "the agent received it and started responding"
     signal. No transcript advance => a typed BLOCKER, never a soft
     "delivered".

Precondition: the Mac's Cursor must be launched with
`--remote-debugging-port=<config.cursor_cdp_port>`. Enable it once with
`ops/cursor_ide_debug.sh`. When the port is closed the driver returns a typed
`precondition` blocker naming that one fix — it does **not** fall back to a
blind paste (`halt-dont-heal`).

CDP transport is proven on Cursor 3.7.x (the IDE honors the flag and exposes a
DevTools endpoint). The composer selectors and the send key are validated
against the live signed-in IDE the moment the port is enabled; until then the
transcript-advance gate guarantees we never claim an unverified send, so a
wrong guess fails loudly as a blocker instead of silently as a lie.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import aiohttp

from .config import config
from .cursor_external import cursor_project_data_dir

log = logging.getLogger(__name__)

CDP_HOST = "127.0.0.1"


def enable_command(port: int | None = None) -> str:
    """The one-command fix surfaced when the CDP port is closed."""
    p = int(port if port is not None else config.cursor_cdp_port)
    return (
        f"run `bash ops/cursor_ide_debug.sh {p}` once at the Mac — it relaunches "
        "Cursor with the CDP control port so I can drive the IDE for real"
    )


# ---------------------------------------------------------------------------
# The truth gate: the on-disk transcript advancing is the only "landed" signal
# ---------------------------------------------------------------------------

def _latest_transcript_mtime(workspace_root: str) -> float:
    """Newest mtime across this workspace's agent-transcript JSONLs.

    A real advance here is the genuine "the Cursor agent received the message
    and started writing a response" signal — the only thing accepted as
    `landed`. (Same on-disk source the roster reads; appends bump file mtime,
    not the parent dir's, so we stat files.)
    """
    root = os.path.join(cursor_project_data_dir(workspace_root), "agent-transcripts")
    latest = 0.0
    try:
        for sid in os.listdir(root):
            sub = os.path.join(root, sid)
            if not os.path.isdir(sub):
                continue
            for fname in os.listdir(sub):
                if not fname.endswith(".jsonl"):
                    continue
                try:
                    m = os.path.getmtime(os.path.join(sub, fname))
                except OSError:
                    continue
                if m > latest:
                    latest = m
    except OSError:
        return 0.0
    return latest


# ---------------------------------------------------------------------------
# CDP transport
# ---------------------------------------------------------------------------

async def _http_targets(port: int, timeout: float = 4.0) -> list[dict] | None:
    """GET /json/list. Returns the target list, or None when the port is closed
    (connection refused / timeout) — the "CDP not enabled" signal."""
    url = f"http://{CDP_HOST}:{port}/json/list"
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                return data if isinstance(data, list) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        return None


def _pick_workbench_target(targets: list[dict], workspace_root: str) -> dict | None:
    """The page target for this project's workbench window.

    Cursor titles a window with the open project/file; match by the workspace
    basename first, then any workbench renderer. DevTools pages are excluded.
    """
    base = os.path.basename(workspace_root.rstrip("/")).lower()
    pages = [
        t
        for t in targets
        if t.get("type") == "page"
        and t.get("webSocketDebuggerUrl")
        and "devtools://" not in (t.get("url") or "")
    ]
    if base:
        for t in pages:
            if base in (t.get("title") or "").lower():
                return t
    for t in pages:
        if "workbench" in (t.get("url") or "").lower():
            return t
    return pages[0] if pages else None


class _Cdp:
    """Minimal CDP client over one page target's websocket (raw attach, so the
    `Input.*` domain — denied to the sandboxed MCP — is available here)."""

    def __init__(self, ws_url: str):
        self._ws_url = ws_url
        self._id = 0
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    async def __aenter__(self) -> "_Cdp":
        self._session = aiohttp.ClientSession()
        self._ws = await asyncio.wait_for(
            self._session.ws_connect(self._ws_url, max_msg_size=0), timeout=10.0
        )
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self._ws is not None:
                await self._ws.close()
        finally:
            if self._session is not None:
                await self._session.close()

    async def call(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        assert self._ws is not None
        self._id += 1
        mid = self._id
        await self._ws.send_str(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = await self._ws.receive(timeout=max(0.1, deadline - time.monotonic()))
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get("id") == mid:
                if "error" in data:
                    raise RuntimeError(f"CDP {method} error: {data['error']}")
                return data.get("result", {})
        raise asyncio.TimeoutError(f"CDP {method} timed out")


# JS to find Cursor's AI chat composer and focus it. Ordered most- to least-
# specific for Cursor 3.7.x. A wrong guess is caught by the transcript gate
# (a loud blocker), never a false "sent".
_FOCUS_JS = r"""
(function () {
  var sels = [
    '.aislash-editor-input[contenteditable="true"]',
    'div[data-lexical-editor="true"][contenteditable="true"]',
    '.composer-editor [contenteditable="true"]',
    '.chat-input-container [contenteditable="true"]',
    'div.inputarea[contenteditable="true"]',
    'textarea.inputarea',
    '[class*="composer"] [contenteditable="true"]',
    '[aria-label*="Ask"][contenteditable="true"]'
  ];
  for (var i = 0; i < sels.length; i++) {
    var el = document.querySelector(sels[i]);
    if (el) {
      try { el.scrollIntoView(); } catch (e) {}
      el.focus();
      return JSON.stringify({found: true, sel: sels[i], tag: el.tagName});
    }
  }
  return JSON.stringify({found: false});
})()
"""


def _blocker(need: str, *, cls: str = "unverified", **extra) -> dict:
    out = {"ok": False, "_error_class": cls, "route": "cdp_ide", "need": need}
    out.update(extra)
    return out


async def drive_ide_chat(
    workspace_root: str,
    message: str,
    *,
    new_agent: bool = False,
    cdp_port: int | None = None,
    verify_timeout_sec: float = 15.0,
) -> dict:
    """Drive the IDE chat for `workspace_root` and verify the send landed.

    Returns `{"ok": True, "verified_landed": True, ...}` ONLY when the
    transcript advances; otherwise a typed blocker (`_error_class` in
    {precondition, unverified, schema}) naming the one thing needed. Never a
    soft "delivered" — the whole point.
    """
    port = int(cdp_port if cdp_port is not None else config.cursor_cdp_port)
    msg = (message or "").strip()
    if not msg:
        return _blocker("a non-empty message to send", cls="schema")

    targets = await _http_targets(port)
    if targets is None:
        return _blocker(
            "Cursor isn't running with the CDP control port, so I can't drive the "
            "IDE for real and I won't pretend a paste landed — " + enable_command(port),
            cls="precondition",
            blocker="cursor_cdp_disabled",
        )

    target = _pick_workbench_target(targets, workspace_root)
    if target is None:
        return _blocker(
            f"no Cursor IDE window is open for {os.path.basename(workspace_root)!r} on "
            "the CDP port — open that project in Cursor, then retry",
            blocker="no_window",
        )

    title = target.get("title") or ""
    pre_mtime = _latest_transcript_mtime(workspace_root)

    try:
        async with _Cdp(target["webSocketDebuggerUrl"]) as cdp:
            res = await cdp.call(
                "Runtime.evaluate", {"expression": _FOCUS_JS, "returnByValue": True}
            )
            try:
                focus = json.loads((res.get("result") or {}).get("value") or "{}")
            except (TypeError, ValueError):
                focus = {}
            if not focus.get("found"):
                return _blocker(
                    "I found the Cursor window but not its chat composer — click into "
                    "the chat box once, or tell me the concrete action; I won't claim a "
                    "send I can't make",
                    matched=title,
                    blocker="no_composer",
                )
            # Trusted text + Enter. Raw-CDP Input events ARE trusted, so the
            # editor accepts the text and the send keybinding fires.
            await cdp.call("Input.insertText", {"text": msg})
            for kind in ("keyDown", "keyUp"):
                await cdp.call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": kind,
                        "key": "Enter",
                        "code": "Enter",
                        "windowsVirtualKeyCode": 13,
                        "nativeVirtualKeyCode": 13,
                    },
                )
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, OSError, AssertionError) as e:
        log.warning("cdp drive failed for %s: %s", workspace_root, e)
        return _blocker(
            f"the CDP drive failed before I could confirm a send ({str(e)[:160]}); "
            "nothing was claimed as delivered",
            matched=title,
            blocker="cdp_error",
        )

    # The truth gate: only "landed" if the transcript actually advances.
    landed = False
    deadline = time.monotonic() + max(1.0, float(verify_timeout_sec))
    while time.monotonic() < deadline:
        await asyncio.sleep(0.6)
        if _latest_transcript_mtime(workspace_root) > pre_mtime + 0.5:
            landed = True
            break

    if landed:
        return {
            "ok": True,
            "route": "cdp_ide",
            "verified_landed": True,
            "matched": title,
            "chars_sent": len(msg),
            "verify_signal": "transcript_advanced",
        }
    return _blocker(
        "I typed it into the Cursor chat and pressed send, but the thread did not "
        "start responding within the wait — so I will NOT claim it landed. Check the "
        "window, or tell me the next move",
        matched=title,
        chars_sent=len(msg),
        verify_signal="transcript_did_not_advance",
        blocker="not_verified",
    )
