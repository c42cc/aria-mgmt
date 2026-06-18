"""The Capability primitive — Aria's single contract for *doing*.

Root cause this fixes (forensic 2026-06-16). Aria had no model of what it was
able to do. Every action was a raw tool call fired into an executor that
inherited whatever environment the bot launched with, with no precondition
check and no postcondition check. So the bot discovered its own limits by
crashing into them at runtime — `screencapture: command not found` (the binary
sat in /usr/sbin, just off the inherited PATH), the Messages Automation wall,
depleted API credits — and then narrated the crash as a wall the *user* must
fix. Meanwhile correctness was chased with prose ("don't brute-force a send")
and a growing pile of incident-specific loop guards.

A Capability answers two questions mechanically:

    can_i()  -> are the preconditions met?   (binary on PATH, an Automation
                grant, ...). If not, the exact one-command fix is returned and
                the action NEVER fires.
    did_i()  -> is the postcondition observed? (optional). "Done" is only
                emitted when the requested end-state is confirmed.

The preconditions reuse the very checks `preflight.py` already owns (a binary
on PATH, a macOS Automation/TCC grant) — the same probes, moved from boot to
the point of use. No framework: stdlib + a dataclass. The point of this module
is to *remove* moving parts elsewhere, not add a layer.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# 1. The one contracted execution environment.
#
# Standard macOS + Homebrew binary locations. `screencapture` lives in
# /usr/sbin; Homebrew tools (gtimeout, ffmpeg, gh, ...) in /opt/homebrew/bin
# or /usr/local/bin. A subprocess that inherits a login-shell-stripped PATH
# (launchd, a service manager, a non-login shell) misses these — which is
# exactly how `screencapture: command not found` happened with the binary
# sitting right there. Every OS-touching subprocess runs in this env, so the
# PATH is a contract, not an accident of how the bot was started.
# ---------------------------------------------------------------------------

_CONTRACT_PATH_DIRS: tuple[str, ...] = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def contracted_path(base_path: Optional[str] = None) -> str:
    """A PATH guaranteed to contain the standard macOS + Homebrew dirs.

    Existing entries are preserved in order (so a caller's deliberate override
    still wins); each guaranteed dir that exists and isn't already present is
    appended. Never removes anything.
    """
    base = base_path if base_path is not None else os.environ.get("PATH", "")
    out: list[str] = [p for p in base.split(os.pathsep) if p]
    have = set(out)
    for d in _CONTRACT_PATH_DIRS:
        if d not in have and os.path.isdir(d):
            out.append(d)
            have.add(d)
    return os.pathsep.join(out)


def contracted_env(
    base: Optional[dict] = None, extra: Optional[dict] = None
) -> dict:
    """A copy of `base` (default os.environ) with PATH normalized to the
    contract and `extra` overlaid. This is the ONLY environment OS-touching
    subprocesses should run in.
    """
    env = dict(base if base is not None else os.environ)
    if extra:
        env.update(extra)
    env["PATH"] = contracted_path(env.get("PATH", ""))
    return env


# ---------------------------------------------------------------------------
# 2. Preconditions — one checkable prerequisite each.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Precond:
    """A single checkable prerequisite.

    `check()` returns `(ok, fix)`: when `ok` is False, `fix` is the exact,
    actionable one-line remedy surfaced to the user. A precondition that
    *cannot be verified* (e.g. the TCC.db is unreadable) returns `ok=True` —
    it never blocks on uncertainty, it only blocks on a known-bad state.
    """

    label: str
    check: Callable[[], tuple[bool, str]]


def binary(name: str, *, fix: Optional[str] = None) -> Precond:
    """Require an executable resolvable on the contracted PATH."""

    def _c() -> tuple[bool, str]:
        if shutil.which(name, path=contracted_path()):
            return True, ""
        return False, fix or (
            f"`{name}` is not on the execution PATH. Install it "
            f"(e.g. `brew install {name}`), or use the right binary — macOS "
            f"has no GNU `timeout`; use `gtimeout` (coreutils) instead."
        )

    return Precond(f"binary:{name}", _c)


# macOS Automation (kTCCServiceAppleEvents) grant, read directly from the
# user's TCC.db read-only. This is the canonical home for the check that
# preflight.probe_messages_send / probe_contacts each open-coded.
_TCC_DB = os.path.expanduser(
    "~/Library/Application Support/com.apple.TCC/TCC.db"
)
_APP_BUNDLE: dict[str, str] = {
    "Messages": "com.apple.MobileSMS",
    "Contacts": "com.apple.AddressBook",
    "Notes": "com.apple.Notes",
    "Calendar": "com.apple.iCal",
}


def automation_grant(app: str, *, fix: Optional[str] = None) -> Precond:
    """Require the macOS Automation grant to control `app` via Apple events.

    Conservative: if the TCC.db is absent or unreadable we cannot verify, so
    we return ok (never block boot or a call on uncertainty). We only block on
    a row that proves the grant is denied/absent for a python client.
    """
    bundle = _APP_BUNDLE.get(app, app)
    default_fix = (
        f"Grant Automation > {app} to the bot's Python: System Settings > "
        f"Privacy & Security > Automation (approve the one-time prompt at the "
        f"Mac). SIP prevents pre-granting it programmatically; improvising "
        f"another send path won't grant a permission."
    )

    def _c() -> tuple[bool, str]:
        if not os.path.exists(_TCC_DB):
            return True, ""
        try:
            con = sqlite3.connect(f"file:{_TCC_DB}?mode=ro&immutable=1", uri=True)
            rows = con.execute(
                "select client, auth_value from access "
                "where service='kTCCServiceAppleEvents' "
                "and indirect_object_identifier=?",
                (bundle,),
            ).fetchall()
            con.close()
        except Exception:
            return True, ""
        py = [(c, a) for (c, a) in rows if "python" in (c or "").lower()]
        if not py:
            return True, ""  # no python row at all → can't prove denial; allow
        if any(a in (2, 3) for _c2, a in py):
            return True, ""
        return False, fix or default_fix

    return Precond(f"automation:{app}", _c)


# ---------------------------------------------------------------------------
# 3. The Capability.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """A unit of doing: a name, its preconditions, and an optional verify.

    `unmet()` is the gate before acting; `confirm()` is the gate before
    claiming done.
    """

    name: str
    requires: tuple[Precond, ...] = ()
    verify: Optional[Callable[[str], tuple[bool, str]]] = None

    def unmet(self) -> Optional[str]:
        """The one-line fix for the FIRST unmet precondition, or None if all
        are satisfied (or unverifiable, which never blocks)."""
        for p in self.requires:
            ok, fix = p.check()
            if not ok:
                return fix
        return None

    def confirm(self, result_text: str) -> tuple[bool, str]:
        """Run the postcondition against the action's result.

        Returns `(verified, note)`. With no verify defined, returns
        `(False, "")` — an undefined postcondition is *unverified by design*,
        never a silent pass. Callers decide how to phrase an unverified done.
        """
        if self.verify is None:
            return False, ""
        try:
            return self.verify(result_text)
        except Exception as exc:  # a broken verify must not pass as success
            return False, f"postcondition check errored: {exc}"


# ---------------------------------------------------------------------------
# 4. Capability registry — which MCP tools carry which preconditions.
#
# Apple sends/lookups require the matching Automation grant; everything else
# is unconditioned (it runs in the contracted env). Kept tiny and declarative
# on purpose — new capabilities are new rows here, not new loop branches.
# ---------------------------------------------------------------------------

# Markers proving a raw shell command is really an Apple-events send, so the
# same Messages grant gates the brute-force `osascript`/JXA path the prompt
# only *asks* the model not to take. Mirrors outcomes._APPLE_AUTOMATION_MARKERS.
_SHELL_APPLE_SEND_MARKERS: tuple[str, ...] = (
    "osascript",
    "tell application \"messages\"",
    "tell application \"contacts\"",
    "mobilesms",
    "imessage:",
)


def requires_for(server: str, tool: str, args: Optional[dict] = None) -> tuple[Precond, ...]:
    """Preconditions for an MCP tool by server+tool (and args for the shell
    send backstop). Returns an empty tuple for unconditioned tools."""
    t = (tool or "").lower()
    if server == "apple":
        if "contact" in t:
            return (automation_grant("Contacts"),)
        if "message" in t or "send" in t:
            return (automation_grant("Messages"),)
    if server == "shell":
        cmd = ""
        if args:
            cmd = " ".join(str(v) for v in args.values()).lower()
        if cmd and any(m in cmd for m in _SHELL_APPLE_SEND_MARKERS):
            # A hand-rolled Apple-events send hits the SAME Automation wall as
            # the sanctioned tool. Gate it identically so it fails clean with
            # the one fix instead of thrashing.
            return (automation_grant("Messages"),)
    return ()


def capability_for(
    server: str, tool: str, args: Optional[dict] = None
) -> Capability:
    """The Capability for an MCP tool call."""
    return Capability(name=f"{server}.{tool}", requires=requires_for(server, tool, args))


# ---------------------------------------------------------------------------
# 5. Postcondition — "done" must be observed, not asserted.
#
# The forensic failure: the loop returned the model's narration as the result
# ("Done, I emailed it") even when the underlying action never succeeded. We
# can't define a bespoke verify for every tool here, but we CAN catch the
# unambiguous case: a world-changing action whose own result carries a hard
# failure signal, while the model still emitted a final answer. Those get a
# factual "could not confirm" note appended to the answer. Markers are chosen
# to (almost) never appear in a *successful* result, so false caveats are rare.
# ---------------------------------------------------------------------------

_WORLD_CHANGING_HINTS: tuple[str, ...] = (
    "send", "create", "delete", "write", "update", "move", "deploy",
    "modify", "draft", "account",
)

_HARD_FAILURE_MARKERS: tuple[str, ...] = (
    '"_error_class"',
    "permission denied",
    "not allowed to send apple events",
    "not authorized to send apple events",
    "did not respond in time",
    "failed to send",
    "-1743",
    "command not found",
    "no such file",
    "operation not permitted",
    "traceback (most recent call last)",
)

# Tools whose SUCCESS result is known to carry a positive confirmation token
# (a message id / "sent successfully"). For these, the absence of any such
# token means delivery is unconfirmed — the per-send "did it actually land?"
# check, not just "did it explicitly fail?". Scoped to tools whose success
# shape we have actually observed, so a real success is never mislabeled.
# (Apple `messages_chat` has no positive delivery receipt in its result, so it
# is verified by absence-of-failure above, not by a required token.)
_CONFIRM_REQUIRED_TOOLS: tuple[str, ...] = ("send_email",)
_SEND_CONFIRM_TOKENS: tuple[str, ...] = (
    "sent successfully", "message sent", "email sent", "messageid",
    "message id", "delivered",
)


def _is_world_changing(name: str, args: Optional[dict]) -> bool:
    """A call changes the world if its name implies it, or it's an Apple
    `messages_chat`/`contacts` call with a create/send action (the name alone
    carries no hint there — the verb lives in `args.action`)."""
    if any(h in name for h in _WORLD_CHANGING_HINTS):
        return True
    action = str((args or {}).get("action", "")).lower()
    return ("message" in name or "contact" in name) and action in ("create", "send")


def unverified_world_changes(trace: Optional[list[dict]]) -> list[str]:
    """World-changing actions in `trace` we cannot confirm succeeded. Returns
    deduped tool labels for an honest 'could not confirm' note.

    An action is flagged when it is world-changing AND either (a) its result
    carries an unambiguous failure marker, or (b) it is a send whose success
    shape must include a confirmation token yet has none. Reads/searches and
    confirmed sends are never flagged, so the note stays low-false-positive.
    """
    flagged: list[str] = []
    for e in trace or []:
        name = (e.get("tool") or "").lower()
        if not _is_world_changing(name, e.get("args")):
            continue
        result = (e.get("result") or "").lower()
        label = e.get("tool") or "action"
        if any(m in result for m in _HARD_FAILURE_MARKERS):
            if label not in flagged:
                flagged.append(label)
            continue
        if any(t in name for t in _CONFIRM_REQUIRED_TOOLS):
            if not any(tok in result for tok in _SEND_CONFIRM_TOKENS):
                if label not in flagged:
                    flagged.append(label)
    return flagged
