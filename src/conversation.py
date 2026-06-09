"""Shared conversation buffer across all of Aria's transports.

Aria has one identity but two transports: Discord text channels (`#ucs`,
`#ucs-alerts`) and Discord voice (plus the local-mic wake-word path). The
user expects continuity — if they type something in `#ucs`, voice-Aria
should know about it on the next `!join`, and if Aria says something in
voice, text-Aria should be able to reference it later in `#ucs`.

This module owns that continuity. It is the single in-memory place where
turns from both sides are interleaved into one thread.

Failure mode is loud-but-bounded: the buffer is a bounded deque, so we
cannot OOM from runaway sessions. The buffer is **not** persisted; on
restart Aria starts fresh. mem0 is the place for durable facts. This
buffer is the *conversational* context — what was just said.
"""

from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger(__name__)

Role = Literal["user", "aria", "alert", "cursor_event"]
Medium = Literal["text", "voice"]

_MAX_TURNS = 60
_MAX_TURN_CHARS = 2000
_MAX_ALERT_CHARS = 2000
_MAX_CURSOR_EVENT_CHARS = 4000

# Cap on how many cursor_event turns are surfaced to Claude's per-task
# context (as_claude_context). The external Cursor observer can fire
# events every few minutes from each watched workspace; without this
# cap, an 11KB Cursor-watch firehose drowns out the user's actual
# request. The 42c.pw failure showed the task body at 11,463 chars with
# the user's "create an account" request only on the last line.
# Most-recent-first wins so live context still reaches Aria.
_MAX_CURSOR_EVENTS_IN_CLAUDE_CONTEXT = 2


@dataclass(frozen=True)
class Turn:
    role: Role
    medium: Medium
    channel: str
    text: str
    ts: float = field(default_factory=time.time)
    # The request-thread this turn belongs to (Discord thread id). Empty for
    # ambient turns (voice with no thread, alerts, cursor events). The text
    # agent loop filters context to one session_key so two request-threads
    # never bleed into each other; voice keeps reading the whole buffer.
    session_key: str = ""

    def short(self) -> str:
        body = self.text.strip()
        if len(body) > _MAX_TURN_CHARS:
            body = body[:_MAX_TURN_CHARS] + " […truncated]"
        return body


class ConversationBuffer:
    """Bounded, ordered record of recent turns across mediums.

    All public methods are synchronous; the underlying deque is safe to
    mutate from any task running on the bot's event loop because we never
    yield mid-operation.
    """

    def __init__(self, max_turns: int = _MAX_TURNS) -> None:
        self._turns: collections.deque[Turn] = collections.deque(maxlen=max_turns)

    # -- writers --------------------------------------------------------

    def add_user_text(self, channel: str, text: str, session_key: str = "") -> None:
        self._append(Turn(role="user", medium="text", channel=channel, text=text, session_key=session_key))

    def add_aria_text(self, channel: str, text: str, session_key: str = "") -> None:
        self._append(Turn(role="aria", medium="text", channel=channel, text=text, session_key=session_key))

    def add_user_voice(self, channel: str, text: str, session_key: str = "") -> None:
        self._append(Turn(role="user", medium="voice", channel=channel, text=text, session_key=session_key))

    def add_aria_voice(self, channel: str, text: str, session_key: str = "") -> None:
        self._append(Turn(role="aria", medium="voice", channel=channel, text=text, session_key=session_key))

    def add_alert(self, text: str) -> None:
        """Record a system alert (preflight, error, confirmation) so Aria
        can reference it from text conversation.

        Alerts are truncated to `_MAX_ALERT_CHARS` at write time. Boot-time
        preflight reports are multi-kilobyte dumps; if we stored them verbatim
        they would dominate `as_claude_context()` and drown out actual
        conversation. The first line of an alert is what matters for
        'what was that error?' queries; the user can read `#ucs-alerts`
        directly in Discord for the full content.
        """
        body = text.strip()
        if not body:
            return
        if len(body) > _MAX_ALERT_CHARS:
            first_line = body.split("\n", 1)[0]
            body = first_line[:_MAX_ALERT_CHARS]
            if len(text) > len(first_line):
                body = body + " […]"
        self._append(Turn(role="alert", medium="text", channel="#ucs-alerts", text=body))

    def add_cursor_event(self, text: str) -> None:
        """Record a cursor watch event so Aria can recall it on follow-up.

        Distinct from `add_alert` in two ways:

        1. The role is `cursor_event`, and both `as_claude_context` and
           `as_gemini_injection` include cursor events by default. They are
           first-class conversational context — when Corbin asks 'what did
           Cursor do?' Aria needs them visible regardless of the
           `include_alerts` flag. Generic alerts (preflight, confirmations)
           were intentionally hidden to keep them from drowning Claude
           context with boot-time noise; the cursor watch deserves a
           separate channel.

        2. The truncation budget is larger (`_MAX_CURSOR_EVENT_CHARS`) so
           workspace_root, the last transcript turn, and recent plan files
           survive into Aria's resume context.
        """
        body = text.strip()
        if not body:
            return
        if len(body) > _MAX_CURSOR_EVENT_CHARS:
            body = body[:_MAX_CURSOR_EVENT_CHARS] + " […]"
        self._append(Turn(role="cursor_event", medium="text", channel="#cursor-watch", text=body))

    def _append(self, turn: Turn) -> None:
        body = turn.text.strip()
        if not body:
            return
        self._turns.append(turn)

    # -- readers --------------------------------------------------------

    def recent(self, max_turns: int = 10) -> list[Turn]:
        if max_turns <= 0:
            return []
        return list(self._turns)[-max_turns:]

    def last_n_turns(self, n: int) -> list[dict]:
        """Serialize the last N turns as plain dicts for external consumers.

        Used by `CursorExternalObserver`'s `GET /recent_turns` endpoint so
        out-of-process test harnesses can assert on what Aria actually
        heard and said. JSON-safe: each entry is {role, medium, channel,
        text, ts (float seconds since epoch)}.
        """
        if n <= 0:
            return []
        return [
            {
                "role": t.role,
                "medium": t.medium,
                "channel": t.channel,
                "text": t.text,
                "ts": t.ts,
            }
            for t in self.recent(max_turns=n)
        ]

    def as_claude_context(
        self,
        max_turns: int = 10,
        exclude_last: int = 0,
        include_alerts: bool = False,
        session_key: str = "",
    ) -> str:
        """Format recent turns as a context preamble for `do_with_claude`.

        Returns an empty string if there are no turns to surface. The
        caller is expected to prefix this to the user's task.

        `exclude_last` lets the caller drop the just-added user turn so it
        isn't duplicated in the task body.

        `include_alerts` defaults to False — boot-time preflight reports
        and confirmation pings should not be silently prepended to every
        Claude task. The caller can opt in when the user is plausibly
        asking about an alert (e.g. 'what was that error?').

        Cursor watch events (role `cursor_event`) are always included
        regardless of `include_alerts`, but capped at
        `_MAX_CURSOR_EVENTS_IN_CLAUDE_CONTEXT` (most-recent-first). One
        watched workspace firing every few minutes was crowding the real
        user request off the bottom of the context window — the 42c.pw
        failure had the actual ask at char 11,300 of 11,463.
        """
        candidates = self.recent(max_turns + exclude_last + 10)
        if not include_alerts:
            candidates = [t for t in candidates if t.role != "alert"]

        # Per-thread isolation: when a session_key is supplied, the agent loop
        # is running a FOCUSED request in its own Discord thread, so it should
        # see only THIS thread's turns. The ambient Cursor-watch firehose is
        # deliberately excluded here — on 2026-06-09 a `live_visuals_4` watch
        # narration bled into the focused `#keys` tailscale thread because the
        # old carve-out let `cursor_event` flow into every session. Voice and
        # global callers (no session_key) still receive cursor events below.
        # Opted-in alerts (already gated by `include_alerts`) remain ambient.
        # This is the primitive that kills cross-request context bleed: a
        # brand-new thread sees none of a sibling thread's turns nor the
        # watcher's noise, so request B never inherits request A's preamble.
        if session_key:
            candidates = [
                t
                for t in candidates
                if t.role == "alert" or t.session_key == session_key
            ]

        # Cap cursor_event turns *first* so they don't claim slots that
        # the user's actual messages need. We keep the most recent
        # `_MAX_CURSOR_EVENTS_IN_CLAUDE_CONTEXT` cursor_events and drop
        # earlier ones — and only count those when computing the
        # `max_turns + exclude_last` window so user/aria turns still fit.
        cursor_events = [t for t in candidates if t.role == "cursor_event"]
        if len(cursor_events) > _MAX_CURSOR_EVENTS_IN_CLAUDE_CONTEXT:
            keep_ids = {
                id(t)
                for t in cursor_events[-_MAX_CURSOR_EVENTS_IN_CLAUDE_CONTEXT:]
            }
            candidates = [
                t for t in candidates
                if t.role != "cursor_event" or id(t) in keep_ids
            ]

        turns = candidates[-(max_turns + exclude_last):] if max_turns else []
        if exclude_last:
            turns = turns[:-exclude_last]
        if not turns:
            return ""

        lines = ["Recent conversation thread (most recent last):"]
        for t in turns:
            speaker = {
                "user": "User",
                "aria": "You",
                "alert": "System alert",
                "cursor_event": "Cursor watch event",
            }[t.role]
            modifier = (
                f" (via {t.medium} in {t.channel})"
                if t.role in ("user", "aria")
                else ""
            )
            lines.append(f"- {speaker}{modifier}: {t.short()}")
        return "\n".join(lines) + "\n"

    def as_gemini_injection(self, max_turns: int = 10, *, include_alerts: bool = False) -> str:
        """Format recent turns for `gemini.inject_text(..., turn_complete=False)`.

        Used when a voice session starts/resumes — gives Gemini context
        about anything that happened while she wasn't connected (text
        exchanges, alerts).

        `include_alerts` defaults to False — boot-time preflight reports,
        confirmation pings, and `_voice_exit_watchdog` notices flow through
        `add_alert()` and would otherwise be re-injected into Gemini's
        context on every `!join` / pause-resume cycle. They are noise to
        the conversational model and re-trigger the very `do_with_claude`
        calls the audit was trying to deduplicate (L15 fix).

        Cursor watch events (role `cursor_event`) are always included
        regardless of `include_alerts`. A cursor task that finished during
        the pause must reach Aria on resume; without this it was invisible
        once the pending-page queue had been drained.
        """
        candidates = self.recent(max_turns + 5)
        if not include_alerts:
            candidates = [t for t in candidates if t.role != "alert"]
        turns = candidates[-max_turns:] if max_turns else []
        if not turns:
            return ""

        lines = ["[Context from recent conversation thread:]"]
        for t in turns:
            if t.role == "user":
                lines.append(f"User said ({t.medium}, {t.channel}): {t.short()}")
            elif t.role == "aria":
                lines.append(f"You said ({t.medium}, {t.channel}): {t.short()}")
            elif t.role == "cursor_event":
                lines.append(f"Cursor watch event recorded: {t.short()}")
            else:
                lines.append(f"System alert posted: {t.short()}")
        lines.append("[End context. Continue from where the thread left off.]")
        return "\n".join(lines)

    def clear(self) -> None:
        self._turns.clear()

    def __len__(self) -> int:
        return len(self._turns)


conversation = ConversationBuffer()
