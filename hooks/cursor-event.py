#!/usr/bin/env python3
"""Cursor hook forwarder.

Invoked by Cursor (via ~/.cursor/hooks.json) on lifecycle events. Reads the
hook JSON payload from stdin, augments it with the hook type (passed as
argv[1] by the installer), and POSTs it to Aria's local HTTP endpoint.

Stdlib only — no venv, no third-party deps. Designed to be fast and silent:
if Aria's not running, we exit 0 (Cursor must not block on us). If our POST
fails for any other reason, we still exit 0 — failing loud here would crash
the user's Cursor session, which is a worse failure mode than missing a
notification.

The endpoint URL is configurable via env (UCS_CURSOR_EVENT_URL); default is
http://127.0.0.1:8731/cursor-event so it matches the bot's default port.

For Cursor hooks of type "stop" / "subagentStop", we MUST also write the
hook's expected JSON response back to stdout (or nothing). We always write
nothing — Aria narrates externally; she does not inject followup messages
into the user's Cursor sessions.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8731/cursor-event"
TIMEOUT_SEC = 2.0


def main() -> int:
    hook_type = sys.argv[1] if len(sys.argv) > 1 else "unknown"

    try:
        raw = sys.stdin.read()
    except Exception:
        return 0

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {"_raw": raw[:2000]}

    payload["_hook_type"] = hook_type

    url = os.environ.get("UCS_CURSOR_EVENT_URL", DEFAULT_URL)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT_SEC).read()
    except urllib.error.URLError:
        pass
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
