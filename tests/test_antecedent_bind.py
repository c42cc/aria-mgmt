"""§7 — bind the antecedent (operate on the dysfunctional primitive).

The dispatch primitive (`conversation.as_claude_context`) must surface the
most-recent cursor-watch event to a FOCUSED request, so a referential ask ("the
debrief") resolves instead of being dispatched context-starved.

Reproduces R5 (2026-06-24 "Give me the debrief"): a fresh-thread request landed
28s after a #cursor-watch "task completed in live_visuals_4". Before the fix the
engine got the bare words and confabulated an email/calendar standup
(OFF-THE-RAILS, root-cause dispatch-context). After it, the antecedent is bound —
and the cap keeps it the antecedent, never the firehose.
"""

from __future__ import annotations

from src import db
from src.conversation import (
    ConversationBuffer,
    _MAX_CURSOR_EVENTS_IN_CLAUDE_CONTEXT,
)


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "state.db"))
    db.init_db()


def test_r5_fresh_thread_binds_cursor_watch_antecedent(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    buf = ConversationBuffer()
    # the referent: a cursor-watch completion lands in the ambient stream
    buf.add_cursor_event(
        "[Cursor watch] Cursor task completed in live_visuals_4 (thread b3da4f0b)."
    )
    # the user opens a FRESH thread in the room and asks a referential question
    buf.add_user_text(
        channel="#Give me the debrief", text="Give me the debrief",
        session_key="thread-r5", parent_channel="room-ucs",
    )
    ctx = buf.as_claude_context(
        max_turns=10, exclude_last=1,
        session_key="thread-r5", parent_channel="room-ucs",
    )
    # BEFORE the fix this was "" → the engine was starved. It must not be now.
    assert ctx, "fresh-thread referential ask must NOT be dispatched context-starved"
    assert "live_visuals_4" in ctx and "completed" in ctx
    # the just-added user turn is excluded (the caller appends it to the body)
    assert "Give me the debrief" not in ctx


def test_cap_keeps_only_two_most_recent_cursor_events(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    buf = ConversationBuffer()
    for i in range(6):
        buf.add_cursor_event(f"[Cursor watch] event number {i} in live_visuals_4")
    buf.add_user_text(
        channel="#t", text="what just happened?",
        session_key="sk", parent_channel="room",
    )
    ctx = buf.as_claude_context(
        max_turns=10, exclude_last=1, session_key="sk", parent_channel="room",
    )
    # the firehose can't bleed: only the two latest cursor-watch events survive
    assert "event number 5" in ctx and "event number 4" in ctx
    assert "event number 0" not in ctx
    assert ctx.count("Cursor watch event") == _MAX_CURSOR_EVENTS_IN_CLAUDE_CONTEXT


def test_other_room_user_turns_still_excluded(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    buf = ConversationBuffer()
    buf.add_user_text(
        channel="#other", text="secret from another room",
        session_key="other-thread", parent_channel="room-OTHER",
    )
    buf.add_user_text(
        channel="#t", text="my request", session_key="sk", parent_channel="room-MINE",
    )
    ctx = buf.as_claude_context(
        max_turns=10, exclude_last=1, session_key="sk", parent_channel="room-MINE",
    )
    # isolation is intact: another room's user turns never bleed in
    assert "secret from another room" not in ctx


def test_same_room_continuity_preserved(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    buf = ConversationBuffer()
    buf.add_user_text(
        channel="#room", text="back up VibeThinker", session_key="t1", parent_channel="room",
    )
    buf.add_aria_text(
        channel="#room", text="Done, archiving now.", session_key="t1", parent_channel="room",
    )
    # a NEW thread in the SAME room inherits the room's user/aria timeline
    buf.add_user_text(
        channel="#room", text="what's the size?", session_key="t2", parent_channel="room",
    )
    ctx = buf.as_claude_context(
        max_turns=10, exclude_last=1, session_key="t2", parent_channel="room",
    )
    assert "back up VibeThinker" in ctx or "Done, archiving now." in ctx
