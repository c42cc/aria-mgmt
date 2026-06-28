#!/usr/bin/env python3
"""The deterministic notify trigger: Cursor's `stop` hook -> a phone DM.

This is the whole IDE notify path. No bot, no HTTP, no registry, no tailer,
no byte-quiet timer. Cursor fires `stop` exactly when an agent turn hands back
to you — that hook firing is the *contract*, and it is the trigger. We do NOT
re-derive "settled" from a private transcript format (the v1 defect: a 12s
byte-quiet timer that re-armed on the next byte and silently dropped the 7:30
notification). The transcript is read once, best-effort, only to ENRICH the
message; if its format ever drifts the message degrades, delivery never does.

Flow (returns to Cursor in milliseconds):
  1. Only act on `stop` (the main hand-back), nothing else.
  2. Dedup per session by transcript byte-offset (O(1) stat, atomic state) so
     one hand-back == exactly one buzz, even if the hook is delivered twice.
  3. Enrich: project + terminal status + your intent + the agent's last words.
  4. Hand the message to the detached delivery worker (`src/notify_phone.py`)
     and exit 0 — the network never blocks the IDE.

Stdlib only; runs with no venv.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTIFY_PHONE = os.path.join(REPO_ROOT, "src", "notify_phone.py")
STATE_DIR = os.path.expanduser("~/.cursor/aria-notify")
OUTBOX_DIR = os.path.join(STATE_DIR, "outbox")
TAIL_BYTES = 64 * 1024

# Terminal statuses that mean YOU stopped it — not a hand-back worth a buzz.
# Fail-open: we skip ONLY on an explicit cancel signal; anything unknown sends.
_CANCEL_STATUSES = {"cancelled", "canceled", "aborted", "stopped", "interrupted"}


def main() -> int:
    hook_type = sys.argv[1] if len(sys.argv) > 1 else ""
    if hook_type != "stop":
        return 0  # bound to `stop` in hooks.json; ignore anything else

    raw = _read_stdin()
    payload = _parse(raw)
    transcript = payload.get("transcript_path") or ""
    sid = _sid(transcript) or _sid(payload.get("conversation_id", "")) or "unknown"
    workspace = _workspace_root(payload)
    project = os.path.basename(workspace.rstrip("/")) if workspace else "cursor"

    # --- deterministic dedup: deliver only if the transcript grew since last buzz
    size = _size(transcript)
    if not _is_new_handback(sid, size):
        return 0

    status, intent, last_words = _enrich(transcript)
    if status in _CANCEL_STATUSES:
        _commit_offset(sid, size)  # mark seen so we don't reconsider it
        return 0

    content = _format(project, status, intent, last_words)
    _commit_offset(sid, size)
    _dispatch(content, kind=_kind(status), project=project, sid=sid)
    return 0


# --------------------------------------------------------------------------- #
# payload + transcript helpers
# --------------------------------------------------------------------------- #
def _read_stdin() -> str:
    try:
        return sys.stdin.read()
    except Exception:
        return ""


def _parse(raw: str) -> dict:
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}


def _sid(transcript_path: str) -> str:
    if not transcript_path:
        return ""
    base = os.path.basename(transcript_path)
    return base[:-6] if base.endswith(".jsonl") else base


def _workspace_root(payload: dict) -> str:
    for key in ("workspace_root", "workspaceRoot", "cwd", "workspace", "project_root"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    ws = payload.get("workspace_roots") or payload.get("workspaceFolders")
    if isinstance(ws, list) and ws:
        first = ws[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("path") or first.get("uri") or ""
    return ""


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _enrich(transcript: str) -> tuple[str, str, str]:
    """Bounded tail read for (terminal status, your intent, agent's last words).
    Best-effort: any miss degrades the message, never the delivery."""
    status, intent, last_words = "", "", ""
    if not transcript:
        return status, intent, last_words
    try:
        with open(transcript, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            fh.seek(max(0, end - TAIL_BYTES))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return status, intent, last_words

    lines = chunk.splitlines()
    for raw in lines:  # skip a leading partial line implicitly (it won't parse)
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if obj.get("type") == "turn_ended" and obj.get("status"):
            status = str(obj["status"])
        role = obj.get("role")
        if role == "assistant":
            txt = _text_of(obj.get("message") or obj.get("content"))
            if txt:
                last_words = txt
        elif role == "user":
            txt = _text_of(obj.get("message") or obj.get("content"))
            if txt:
                intent = _clean_intent(txt)
    return status, intent, last_words


def _text_of(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, dict):
        for key in ("content", "text", "value", "body"):
            val = node.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, list):
                parts = [_text_of(p) for p in val]
                joined = " ".join(p for p in parts if p)
                if joined.strip():
                    return joined.strip()
        return ""
    if isinstance(node, list):
        return " ".join(_text_of(p) for p in node).strip()
    return ""


def _clean_intent(text: str) -> str:
    """The real ask lives inside <user_query>; without this every thread's
    first turn looks identical (tool dumps, file drops, env preamble)."""
    lo = text.find("<user_query>")
    if lo != -1:
        hi = text.find("</user_query>", lo)
        if hi != -1:
            return text[lo + len("<user_query>"):hi].strip()
    return text.strip()


# --------------------------------------------------------------------------- #
# dedup state (per-sid, atomic)
# --------------------------------------------------------------------------- #
def _state_path(sid: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)[:80]
    return os.path.join(STATE_DIR, f"sid_{safe}.json")


def _is_new_handback(sid: str, size: int) -> bool:
    if size <= 0:
        return True  # no transcript to measure -> the hook firing is the truth
    try:
        with open(_state_path(sid), "r", encoding="utf-8") as fh:
            offset = int(json.load(fh).get("offset", 0))
    except (OSError, ValueError):
        offset = 0
    return size > offset


def _commit_offset(sid: str, size: int) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = _state_path(sid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"offset": size, "at": time.time()}, fh)
        os.replace(tmp, _state_path(sid))
    except OSError as exc:
        sys.stderr.write(f"[notify-finish] state write failed for {sid}: {exc}\n")


# --------------------------------------------------------------------------- #
# message-as-product
# --------------------------------------------------------------------------- #
def _kind(status: str) -> str:
    if status in ("error", "errored", "failed"):
        return "errored"
    return "finished"


def _format(project: str, status: str, intent: str, last_words: str) -> str:
    mention = (_first_user_id() or "").strip()
    prefix = f"<@{mention}> " if mention else ""
    if status in ("error", "errored", "failed"):
        head = f"\u26a0\ufe0f {project} \u2014 errored"
    elif status in ("success", "", "completed", "finished"):
        head = f"\u2705 {project} \u2014 finished"
    else:
        head = f"\u2705 {project} \u2014 handed back ({status})"
    lines = [f"{prefix}{head}"]
    if intent:
        lines.append(f"You asked: {_truncate(intent, 220)}")
    if last_words:
        lines.append(f"Last: {_truncate(last_words, 320)}")
    return "\n".join(lines)


def _truncate(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _first_user_id() -> str:
    # Read straight from .env so the hook stays self-contained (no src.config).
    path = os.path.join(REPO_ROOT, ".env")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("AUTHORIZED_USER_IDS="):
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return _strip_comment(raw).split(",")[0].strip()
    except OSError:
        pass
    return os.environ.get("AUTHORIZED_USER_IDS", "").split(",")[0].strip()


def _strip_comment(val: str) -> str:
    """Drop an inline `# comment` (a `#` preceded by whitespace) so an id never
    carries the comment into a mention/URL."""
    for i, ch in enumerate(val):
        if ch == "#" and (i == 0 or val[i - 1].isspace()):
            return val[:i].strip()
    return val


# --------------------------------------------------------------------------- #
# detached hand-off (the network never blocks the IDE)
# --------------------------------------------------------------------------- #
def _dispatch(content: str, *, kind: str, project: str, sid: str) -> None:
    spec = {"content": content, "kind": kind, "project": project, "sid": sid}
    try:
        os.makedirs(OUTBOX_DIR, exist_ok=True)
        outbox = os.path.join(OUTBOX_DIR, f"{sid}_{int(time.time()*1000)}.json")
        with open(outbox, "w", encoding="utf-8") as fh:
            json.dump(spec, fh)
    except OSError as exc:
        sys.stderr.write(f"[notify-finish] outbox write failed: {exc}\n")
        return _inline_deliver(spec)

    try:
        subprocess.Popen(
            [sys.executable, NOTIFY_PHONE, "deliver", outbox],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        sys.stderr.write(f"[notify-finish] detached spawn failed: {exc}\n")
        _inline_deliver(spec)


def _inline_deliver(spec: dict) -> None:
    """Spawn failed (rare). Deliver inline through the SAME home rather than
    drop it — one delivery primitive, never a second silent path."""
    try:
        sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
        import notify_phone  # type: ignore

        notify_phone.deliver(
            spec["content"], kind=spec["kind"], project=spec["project"], sid=spec["sid"]
        )
    except Exception as exc:
        sys.stderr.write(f"[notify-finish] inline delivery failed: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
