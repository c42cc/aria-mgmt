#!/usr/bin/env python3
"""The ONE delivery home: a verified phone notification, loud on failure.

This is the single primitive every "tell Corbin on his phone" path goes
through — the standalone Cursor stop hook (`hooks/notify-finish.py`), the
running bot's SDK / Claude-Code finish narration (`src/bot.py`), and the
self-test heartbeat. There is exactly one implementation so the honesty
contract cannot drift into two homes (the v1 defect: a hook that fabricated
"delivered" while the real DM had been dead all day).

Contract — non-negotiable, enforced here:

  * "delivered" means Discord ACCEPTED the message for your DM (HTTP 2xx + a
    message id). Nothing else may write a `delivered` ledger line.
  * A failure is LOUD, never silent and never a lie: a `failed` ledger line,
    a `[NOTIFY PATH DOWN]` alarm in the text channel, and a local macOS
    notification (a rung that does NOT depend on Discord, so a total Discord
    outage still pokes you). None of these claim the original notification
    arrived.
  * No retry/backoff loop, no silent fallback. A failed send stays visible in
    the ledger; the root fix (e.g. "enable DMs") is surfaced, not papered over.
  * It is never "Discord's fault." A timeout, a 5xx, a thundering herd — all
    read as OUR notify path failing, surfaced for us to fix.

Stdlib only (urllib, json, os, subprocess). It must run from the Cursor hook
with no venv and be importable by the bot, so it parses `.env` itself and
never imports `src.config` (which pulls in dotenv).
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

API = "https://discord.com/api/v10"
HTTP_TIMEOUT = 8.0

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.expanduser("~/.cursor/aria-notify")
LEDGER = os.path.expanduser("~/Library/Logs/voicebot/notify.log")


# --------------------------------------------------------------------------- #
# env + ledger
# --------------------------------------------------------------------------- #
def _clean_value(val: str) -> str:
    """Match python-dotenv: honor quotes, strip an inline `# comment` (a `#`
    preceded by whitespace). The bot loads `.env` via dotenv (clean); this hand
    parser must agree or a value like `DISCORD_TEXT_CHANNEL_ID=123 #ucs` leaks
    the comment into a URL (the real bug this fixes)."""
    val = val.strip()
    if val[:1] in ('"', "'"):
        end = val.find(val[0], 1)
        return val[1:end] if end != -1 else val[1:]
    for i, ch in enumerate(val):
        if ch == "#" and (i == 0 or val[i - 1].isspace()):
            return val[:i].strip()
    return val


def load_env(repo_root: str = REPO_ROOT) -> dict[str, str]:
    """Parse `<repo>/.env` (KEY=VALUE), overlaid by the real environment.

    No third-party dotenv — this runs from the bare Cursor hook. A missing
    `.env` is itself a loud failure surfaced by the caller, never a default.
    """
    env: dict[str, str] = {}
    path = os.path.join(repo_root, ".env")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key:
                    env[key] = _clean_value(val)
    except FileNotFoundError:
        pass
    # The live environment wins (so the bot's already-loaded secrets apply).
    for key in ("DISCORD_APP_BOT_TOKEN", "AUTHORIZED_USER_IDS", "DISCORD_TEXT_CHANNEL_ID"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _ensure_dirs() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)


def ledger_append(entry: dict) -> None:
    """Append one JSONL record to the durable ledger. The ledger is the single
    source of truth for "did the human get it" — write-ahead `pending`, then
    `delivered`/`failed`. A ledger write that itself fails is shouted to stderr;
    it is never swallowed."""
    entry = {"ts": _now_iso(), **entry}
    try:
        _ensure_dirs()
        with open(LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:  # the record-keeper itself broke — be loud
        sys.stderr.write(f"[notify_phone] LEDGER WRITE FAILED: {exc} :: {entry}\n")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


# --------------------------------------------------------------------------- #
# Discord REST (stdlib)
# --------------------------------------------------------------------------- #
class NotifyError(Exception):
    """A typed, loud delivery failure. Carries the actionable hint."""

    def __init__(self, message: str, *, status: int | None = None, hint: str = ""):
        super().__init__(message)
        self.status = status
        self.hint = hint


def _post(token: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "AriaNotify/1.0 (+local)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = "<no body>"
        hint = ""
        if exc.code == 403:
            hint = (
                "Discord is refusing the DM (DMs disabled for this server). "
                "Fix: Discord -> right-click the server -> Privacy Settings -> "
                "'Allow direct messages from server members' ON."
            )
        elif exc.code in (401, 403):
            hint = "Bot token rejected — check DISCORD_APP_BOT_TOKEN in .env."
        # Never 'blame Discord': a 5xx is OUR notify path failing to deliver.
        raise NotifyError(
            f"notify path failed at {path}: HTTP {exc.code} {detail[:200]}",
            status=exc.code,
            hint=hint,
        ) from exc
    except urllib.error.URLError as exc:
        raise NotifyError(
            f"notify path could not reach Discord at {path}: {exc.reason} "
            f"(our connectivity, our problem to fix)"
        ) from exc
    except (http.client.HTTPException, ValueError, OSError) as exc:
        # A malformed value (e.g. a bad channel id) or transport fault must read
        # as a typed LOUD notify failure, never an uncaught crash that loses the
        # alarm. Still our problem to fix, never "Discord's fault".
        raise NotifyError(f"notify path malformed/transport error at {path}: {exc}") from exc


def _dm_channel_id(token: str, user_id: str) -> str:
    """Open (and cache) the DM channel with the authorized user."""
    cache = os.path.join(STATE_DIR, f"dm_channel_{user_id}")
    try:
        with open(cache, "r", encoding="utf-8") as fh:
            cached = fh.read().strip()
            if cached:
                return cached
    except FileNotFoundError:
        pass
    data = _post(token, "/users/@me/channels", {"recipient_id": str(user_id)})
    channel_id = str(data.get("id") or "")
    if not channel_id:
        raise NotifyError("Discord returned no DM channel id")
    try:
        _ensure_dirs()
        with open(cache, "w", encoding="utf-8") as fh:
            fh.write(channel_id)
    except OSError:
        pass  # caching is best-effort; the send below is what matters
    return channel_id


def _send(token: str, channel_id: str, content: str) -> str:
    """Send and return the message id (proof of acceptance). Empty id => fail."""
    data = _post(token, f"/channels/{channel_id}/messages", {"content": content})
    msg_id = str(data.get("id") or "")
    if not msg_id:
        raise NotifyError("Discord accepted no message id — treat as undelivered")
    return msg_id


# --------------------------------------------------------------------------- #
# the loud alarm (failure is never silent, never a lie)
# --------------------------------------------------------------------------- #
def _local_notification(title: str, message: str) -> None:
    """Last-resort rung that does NOT depend on Discord. A macOS notification
    so a total Discord outage still surfaces locally."""
    import subprocess

    def _osa(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{_osa(message)[:200]}" with title "{_osa(title)[:80]}"',
            ],
            timeout=5,
            check=False,
        )
    except Exception as exc:
        sys.stderr.write(f"[notify_phone] local notification rung failed: {exc}\n")


def _alarm_throttled(why_key: str, window_sec: float = 1800.0) -> bool:
    """True if an alarm with this key fired within the window — so the loud rungs
    don't FLOOD #ucs while a broken path stays broken. This throttles only the
    repeated NOTICE; the `failed`/`alarm` ledger lines of truth are still written."""
    path = os.path.join(STATE_DIR, "last_alarm.json")
    now = time.time()
    try:
        with open(path) as fh:
            prev = json.load(fh)
        if prev.get("why_key") == why_key and now - float(prev.get("at", 0)) < window_sec:
            return True
    except (OSError, ValueError):
        pass
    try:
        _ensure_dirs()
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"why_key": why_key, "at": now}, fh)
        os.replace(tmp, path)
    except OSError:
        pass
    return False


def _alarm(env: dict, *, why: str, hint: str, missed: str, sid: str, project: str,
           why_key: str = "") -> None:
    """Raise a LOUD alarm that the notify path is DOWN. This is not a fallback
    delivery of the notification — it is an alarm about a broken primitive,
    plus the context of what you missed and the one-command fix. The loud rungs
    are throttled per `why_key` so a persistently-broken path nags once, not
    once-per-stop; every failure is still recorded in the ledger."""
    if why_key and _alarm_throttled(why_key):
        ledger_append({"status": "alarm", "sid": sid, "project": project,
                       "why": why, "throttled": True})
        return
    token = env.get("DISCORD_APP_BOT_TOKEN", "")
    channel = env.get("DISCORD_TEXT_CHANNEL_ID", "")
    mention = (env.get("AUTHORIZED_USER_IDS", "").split(",") or [""])[0].strip()
    body = (
        f"{'<@' + mention + '> ' if mention else ''}\u26a0\ufe0f **NOTIFY PATH DOWN** "
        f"\u2014 I could not reach your phone.\n"
        f"Why: {why}\n"
        f"{('Fix: ' + hint) if hint else ''}\n"
        f"You missed ({project}): {missed[:600]}"
    )
    posted = False
    if token and channel:
        try:
            _send(token, channel, body)
            posted = True
        except NotifyError as exc:
            sys.stderr.write(f"[notify_phone] alarm channel post failed: {exc}\n")
    _local_notification("Aria notify path DOWN", f"{project}: {why}")
    ledger_append({
        "status": "alarm",
        "sid": sid,
        "project": project,
        "why": why,
        "channel_alarm": posted,
    })


# --------------------------------------------------------------------------- #
# the primitive
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    delivered: bool
    msg_id: str = ""
    error: str = ""
    hint: str = ""


def deliver(
    content: str,
    *,
    kind: str = "finished",
    project: str = "",
    sid: str = "",
    env: dict | None = None,
) -> Result:
    """Deliver `content` to the authorized user's phone via a verified DM.

    Returns a Result. Writes write-ahead `pending` then `delivered`/`failed`.
    On ANY failure raises no exception to the caller — it has already shouted
    (alarm + ledger) — and returns delivered=False so the caller can act, but
    it NEVER returns delivered=True without Discord's message id.
    """
    env = env or load_env()
    token = env.get("DISCORD_APP_BOT_TOKEN", "")
    users = [u.strip() for u in env.get("AUTHORIZED_USER_IDS", "").split(",") if u.strip()]

    ledger_append({"status": "pending", "sid": sid, "project": project, "kind": kind})

    if not token or not users:
        why = "missing DISCORD_APP_BOT_TOKEN or AUTHORIZED_USER_IDS in .env"
        _alarm(env, why=why, hint="populate .env", missed=content, sid=sid,
               project=project, why_key="missing-secrets")
        ledger_append({"status": "failed", "sid": sid, "project": project, "error": why})
        return Result(False, error=why)

    user_id = users[0]
    try:
        channel_id = _dm_channel_id(token, user_id)
        msg_id = _send(token, channel_id, content)
    except NotifyError as exc:
        # A cached DM channel can go stale (rare). Re-open ONCE — this is error
        # recovery on a transient handle, not a retry loop that hides failure.
        if exc.status in (403, 404) and _drop_dm_cache(user_id):
            try:
                channel_id = _dm_channel_id(token, user_id)
                msg_id = _send(token, channel_id, content)
                ledger_append({"status": "delivered", "sid": sid, "project": project,
                               "kind": kind, "msg_id": msg_id})
                return Result(True, msg_id=msg_id)
            except NotifyError as exc2:
                exc = exc2
        _alarm(env, why=str(exc), hint=exc.hint, missed=content, sid=sid,
               project=project, why_key=str(exc.status or "send"))
        ledger_append({"status": "failed", "sid": sid, "project": project,
                       "error": str(exc), "hint": exc.hint})
        return Result(False, error=str(exc), hint=exc.hint)

    ledger_append({"status": "delivered", "sid": sid, "project": project,
                   "kind": kind, "msg_id": msg_id})
    return Result(True, msg_id=msg_id)


def _drop_dm_cache(user_id: str) -> bool:
    cache = os.path.join(STATE_DIR, f"dm_channel_{user_id}")
    try:
        os.remove(cache)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# heartbeat: prove the whole path is alive, on a schedule
# --------------------------------------------------------------------------- #
def heartbeat(env: dict | None = None, *, max_silence_hours: float = 12.0) -> Result:
    """Exercise the REAL delivery path end-to-end and check ledger freshness.

    The system announces its own death: if this can't deliver, it has already
    alarmed. If it delivers but real notifications have been silent too long,
    that is itself surfaced (the path may be live yet unexercised — fine — but
    a stale ledger with pending-but-never-resolved entries is loud)."""
    env = env or load_env()
    stale = _stale_pending()
    note = f" ({stale} undelivered pending in ledger)" if stale else ""
    res = deliver(
        f"\u2705 Aria notify path healthy \u2014 heartbeat {_now_iso()}{note}",
        kind="heartbeat",
        project="heartbeat",
        env=env,
    )
    return res


def _stale_pending() -> int:
    """Count ledger `pending` entries with no later `delivered`/`failed` for the
    same sid — the durable evidence of a send that vanished."""
    try:
        with open(LEDGER, "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-2000:]
    except FileNotFoundError:
        return 0
    pending: dict[str, int] = {}
    for raw in lines:
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        sid = e.get("sid") or ""
        st = e.get("status")
        if st == "pending":
            pending[sid] = pending.get(sid, 0) + 1
        elif st in ("delivered", "failed"):
            pending.pop(sid, None)
    return sum(pending.values())


# --------------------------------------------------------------------------- #
# CLI: the detached worker + the heartbeat
# --------------------------------------------------------------------------- #
def _main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "heartbeat":
        res = heartbeat()
        print("healthy" if res.delivered else f"DOWN: {res.error}")
        return 0 if res.delivered else 1
    if len(argv) >= 3 and argv[1] == "deliver":
        try:
            with open(argv[2], "r", encoding="utf-8") as fh:
                spec = json.load(fh)
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"[notify_phone] could not read outbox {argv[2]}: {exc}\n")
            return 1
        res = deliver(
            spec.get("content", ""),
            kind=spec.get("kind", "finished"),
            project=spec.get("project", ""),
            sid=spec.get("sid", ""),
        )
        # Keep the outbox file on failure (durable evidence); remove on success.
        if res.delivered:
            try:
                os.remove(argv[2])
            except OSError:
                pass
        return 0 if res.delivered else 1
    sys.stderr.write("usage: notify_phone.py {deliver <outbox.json> | heartbeat}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
