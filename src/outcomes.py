"""Deterministic tool-outcome classifier — the single seam where the agent
loop decides whether to continue, retry once, or stop.

The dysfunctional primitive this replaces (forensic 2026-06-09, the spark2-SSH
grind): the loop trusted a *count* of failures to notice a permanent wall, and
read the failure's *exit code* to decide whether it had failed at all. A
wrapper that exited 0 while printing `Permission denied (publickey)` defeated
both — so a wall became a 30-iteration, ~$20 grind that ended in the
spec-FAILED string "Task reached iteration limit". This module reads the
*meaning* of a result instead, independent of exit code, and returns one of
three outcomes:

    PROGRESS          — success, useful data, or a recoverable failure the
                        model should adapt to; continue the loop.
    TRANSIENT(reason) — momentary (timeout / reset / rate-limit / 5xx);
                        allow one bounded retry of that family, then BLOCK.
    BLOCKED(reason,    — a permanent wall (auth / permission denied / host-key
            need)        / EXIT:255 / not authorized / 401 / 403 / command not
                         found / no such file / unreachable) or a user decline;
                         stop now and surface the one thing needed to proceed.

Pure and dependency-free (stdlib only) so the live legacy loop in
`src/tools.py` and the dormant UCS loop in `src/ucs.py` call the same policy.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

PROGRESS = "progress"
TRANSIENT = "transient"
BLOCKED = "blocked"

# One bounded retry per transient family before we treat it as a wall.
TRANSIENT_RETRY_BUDGET = 1

# The single thing a BLOCKED result needs, by kind.
_DECLINE_NEED = (
    "your approval — reply `!ok <action_id>` to the confirmation card in "
    '#ucs-alerts (or say "yes" in voice), then send the task again'
)
_PERMISSION_NEED = "the missing OS/OAuth permission named above — grant it, then I'll retry"
_WALL_NEED = (
    "the missing access / credential / permission for that step — or a "
    "different approach"
)


@dataclass(frozen=True)
class Outcome:
    """What the loop should do with a tool result.

    `family` keys the per-family transient retry budget; `need` is the single
    thing a BLOCKED result requires to proceed (surfaced to the user verbatim).
    """

    kind: str
    reason: str = ""
    need: str = ""
    family: str = ""

    @property
    def is_progress(self) -> bool:
        return self.kind == PROGRESS

    @property
    def is_transient(self) -> bool:
        return self.kind == TRANSIENT

    @property
    def is_blocked(self) -> bool:
        return self.kind == BLOCKED


# Permanent walls — retrying cannot fix these; only a credential, a permission,
# a different host/path, or a human decision can. Matched as lowercase
# substrings against the *text* of a result that already looks like a failure,
# so an exitCode:0 wrapper that prints the real error is still caught.
_BLOCKED_PATTERNS: tuple[str, ...] = (
    "permission denied",
    "publickey",
    "host key verification failed",
    "host key verification",
    "not authorized",
    "unauthorized",
    "403 forbidden",
    "401 unauthorized",
    " 401 ",
    " 403 ",
    "access denied",
    "operation not permitted",
    "command not found",
    "no such file or directory",
    "no such file",
    "no route to host",
    "could not resolve host",
    "name or service not known",
    "network is unreachable",
    "host is unreachable",
    "authentication failed",
    "invalid credentials",
    "login failed",
)

# Momentary — one retry is reasonable, then treat as a wall.
_TRANSIENT_PATTERNS: tuple[str, ...] = (
    "did not respond in time",
    "did not respond",
    "connection reset",
    "connection refused",
    "econnreset",
    "econnrefused",
    "etimedout",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    " 502 ",
    " 503 ",
    " 504 ",
    "rate limit",
    "ratelimit",
    "too many requests",
    " 429 ",
    "retry-after",
)

# SSH and many CLIs surface a hard failure as exit status 255. Treated as a
# wall per the forensic (the grind was SSH auth). Matches `EXIT:255`,
# `exit code 255`, `"exitCode": 255`, `exit_code=255` — but not 2550.
_EXIT_255 = re.compile(r"exit[\s_]*(?:code)?[\"'\s:=]*255(?!\d)", re.IGNORECASE)

# Any embedded non-zero exit marker — used only to decide a result *looks like*
# a failure (so we scan its text), independent of the wrapper's own exit field.
_EXIT_ANY = re.compile(r"exit[\s_]*(?:code)?[\"'\s:=]*(-?\d+)", re.IGNORECASE)


def _parse_envelope(s: str) -> dict | None:
    t = (s or "").lstrip()
    if not t.startswith("{"):
        return None
    try:
        obj = json.loads(t)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _action_family(tool_name: str, args: dict | None) -> str:
    """Coarse 'same KIND of action' signature for the transient retry budget.

    Collapses `ssh a@b` / `ssh c@d.local` / `sudo ssh e@f` into one `exec:ssh`
    family so a family's single retry isn't reset by surface variation; leading
    `VAR=val` env-assignments and common wrappers are stripped so the real verb
    wins. Non-shell tools key on the tool name. (Lives here, with the
    classifier, so the policy is self-contained.)
    """
    if tool_name == "execute_command":
        cmd = str((args or {}).get("command", ""))
        for raw in cmd.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            toks = line.split()
            i = 0
            while i < len(toks) and not toks[i].startswith("-") and "=" in toks[i].split("/", 1)[0]:
                i += 1
            while i < len(toks) and toks[i] in ("sudo", "command", "exec", "env", "time", "nohup"):
                i += 1
            verb = toks[i] if i < len(toks) else (toks[0] if toks else "?")
            return f"exec:{verb}"
        return "exec:?"
    return f"tool:{tool_name}"


def _short_reason(s: str) -> str:
    """A short, human error string out of a failed tool result."""
    obj = _parse_envelope(s)
    if obj is not None:
        for k in ("stderr", "_message", "_raw", "error", "message"):
            v = obj.get(k)
            if v:
                return str(v).strip().replace("\n", " ")[:300]
        ec = obj.get("exitCode", obj.get("exit_code"))
        if ec not in (None, 0, "0"):
            return f"exit code {ec}"
    return (s or "").strip().replace("\n", " ")[:300]


def _embedded_nonzero_exit(low: str) -> bool:
    for m in _EXIT_ANY.finditer(low):
        try:
            if int(m.group(1)) != 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _looks_like_failure(obj: dict | None, low: str) -> bool:
    """True if a result looks like a tool *failure* — so we scan its text for a
    wall. Guards the meaning-scan against false positives on successful output
    that merely mentions a trigger word (an email body containing "timeout").
    """
    if obj is not None:
        ec = obj.get("exitCode", obj.get("exit_code"))
        if isinstance(ec, bool):
            pass
        elif isinstance(ec, (int, float)) and ec != 0:
            return True
        elif isinstance(ec, str) and ec.strip().lstrip("-").isdigit() and int(ec) != 0:
            return True
        if str(obj.get("stderr") or "").strip():
            return True
    if _embedded_nonzero_exit(low):
        return True
    if "command failed" in low or "fatal:" in low:
        return True
    return False


def _match(low: str, patterns: tuple[str, ...]) -> bool:
    return any(p in low for p in patterns)


def classify_outcome(tool_name: str, args: dict | None, result_str: str) -> Outcome:
    """Classify one tool result into PROGRESS / TRANSIENT / BLOCKED by meaning.

    The exit code is never trusted on its own: a result is only *scanned* for a
    wall once it looks like a failure, and the wall test is the failure text —
    so `Permission denied (publickey)` is BLOCKED whether the wrapper exits 0
    or 255.
    """
    s = (result_str or "").strip()
    if not s:
        return Outcome(PROGRESS)

    obj = _parse_envelope(s)
    # The dedup ledger already tells the model to stop repeating a call; a
    # cached-result marker is not itself a failure.
    if obj is not None and obj.get("_dup_hit"):
        return Outcome(PROGRESS)

    family = _action_family(tool_name, args)
    low = s.lower()[:8000]

    # 1. A typed MCP error envelope (src/mcp.py::_typed_error) is decisive.
    if obj is not None and obj.get("_error_class"):
        cls = obj["_error_class"]
        message = _short_reason(s)
        if cls == "declined":
            return Outcome(BLOCKED, message or "the action was declined", _DECLINE_NEED, family)
        if cls == "permission":
            return Outcome(BLOCKED, message or "permission denied", _PERMISSION_NEED, family)
        if cls in (TRANSIENT, "rate_limit"):
            return Outcome(TRANSIENT, message or cls, "", family)
        # schema / unknown: the model can re-read the schema or surface it.
        return Outcome(PROGRESS)

    # 2. Failure-gated meaning scan (defeats the exitCode:0 masking).
    if _looks_like_failure(obj, low):
        if _match(low, _BLOCKED_PATTERNS) or _EXIT_255.search(low):
            return Outcome(BLOCKED, _short_reason(s), _WALL_NEED, family)
        if _match(low, _TRANSIENT_PATTERNS):
            return Outcome(TRANSIENT, _short_reason(s), "", family)

    # 3. No wall signal: success or a recoverable failure the model adapts to.
    #    The iteration cap and per-loop cost cap remain the backstops.
    return Outcome(PROGRESS)


def format_block(reason: str, need: str) -> str:
    """The single user-facing blocker message — replaces the separate stuck,
    decline, and cost formatters. Names what failed and the one thing needed,
    because a crisp blocker the user can act on beats a long grind that ends in
    'partial progress'.
    """
    r = (reason or "").strip().replace("\n", " ")
    if len(r) > 300:
        r = r[:300] + " […]"
    n = (need or "tell me how you'd like to proceed").strip()
    return (
        "**Blocked — I hit a wall and stopped instead of grinding.**\n"
        f"What failed: {r}\n"
        f"What I need to proceed: {n}.\n"
        "I did not keep guessing — say the word and I'll continue."
    )
