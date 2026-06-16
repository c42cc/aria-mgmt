"""Durable conversation log of record (Primitive 3, step 1).

Proves the buffer is no longer memory-only: turns persist and a fresh buffer
(post-restart) reloads them — the direct fix for "on restart Aria starts
fresh" (forensic 2026-06-16).
"""

from __future__ import annotations

from src import db
from src.conversation import ConversationBuffer


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "state.db"))
    db.init_db()


def test_append_and_read_roundtrip(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    db.append_conversation_turn("user", "text", "#ucs", "hello", session_key="s1")
    db.append_conversation_turn("aria", "text", "#ucs", "hi back", session_key="s1")
    db.append_conversation_turn("user", "text", "#other", "elsewhere", session_key="s2")
    allrows = db.recent_conversation_turns(limit=10)
    assert [r["text"] for r in allrows] == ["hello", "hi back", "elsewhere"]
    s1 = db.recent_conversation_turns(limit=10, session_key="s1")
    assert [r["role"] for r in s1] == ["user", "aria"]


def test_empty_text_is_noop(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    db.append_conversation_turn("user", "text", "#ucs", "   ", session_key="s1")
    assert db.recent_conversation_turns(limit=10) == []


def test_buffer_writes_through_to_log(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    buf = ConversationBuffer()
    buf.add_user_text(channel="#ucs", text="durable?", session_key="sk", parent_channel="p")
    buf.add_aria_text(channel="#ucs", text="yes durable", session_key="sk", parent_channel="p")
    rows = db.recent_conversation_turns(limit=10, session_key="sk")
    assert [(r["role"], r["text"]) for r in rows] == [
        ("user", "durable?"),
        ("aria", "yes durable"),
    ]


def test_hydrate_reloads_tail_into_empty_buffer(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    db.append_conversation_turn("user", "text", "#ucs", "before restart", session_key="sk")
    db.append_conversation_turn("aria", "text", "#ucs", "noted", session_key="sk")
    # a fresh buffer (post-restart) hydrates from the durable log
    buf = ConversationBuffer()
    assert len(buf) == 0
    assert buf.hydrate(limit=10) == 2
    assert [t.text for t in buf.recent(max_turns=10)] == ["before restart", "noted"]
    # hydrate never duplicates live turns
    assert buf.hydrate(limit=10) == 0
