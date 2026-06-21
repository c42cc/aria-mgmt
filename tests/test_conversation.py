"""The durable conversation store — memory, alternation, and multi-thread."""

from __future__ import annotations

from src import conversation


def test_append_and_thread_messages():
    conversation.append(thread="t", session="s", channel="text", role="user", content="hello")
    conversation.append(thread="t", session="s", channel="text", role="aria", content="hi there", phase="CHITCHAT", latency_ms=10)
    assert conversation.thread_messages("t") == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


def test_leading_assistant_dropped_and_same_role_merged():
    # a leading assistant turn is invalid as the first API message -> dropped
    conversation.append(thread="t", session="s", channel="text", role="aria", content="leading")
    conversation.append(thread="t", session="s", channel="text", role="user", content="u1")
    conversation.append(thread="t", session="s", channel="text", role="observation", content="o1")
    msgs = conversation.thread_messages("t")
    assert msgs[0]["role"] == "user"
    assert "u1" in msgs[0]["content"] and "o1" in msgs[0]["content"]  # consecutive user-side merged


def test_other_threads_context_excludes_current():
    conversation.append(thread="main", session="s", channel="text", role="user", content="in main")
    conversation.append(thread="side", session="s", channel="text", role="user", content="in the side thread")
    ctx = conversation.other_threads_context("main")
    assert "side" in ctx and "in the side thread" in ctx
    assert "in main" not in ctx


def test_cross_session_continuity():
    conversation.append(thread="main", session="A", channel="text", role="user", content="my favorite color is teal")
    conversation.append(thread="main", session="A", channel="text", role="aria", content="noted")
    # a later session reads the same thread and sees session A
    joined = " ".join(m["content"] for m in conversation.thread_messages("main"))
    assert "teal" in joined


def test_window_caps_turns():
    for i in range(10):
        conversation.append(thread="t", session="s", channel="text", role="user", content=f"u{i}")
        conversation.append(thread="t", session="s", channel="text", role="aria", content=f"a{i}")
    msgs = conversation.thread_messages("t", limit=4)
    assert len(msgs) <= 4
    assert "a9" in msgs[-1]["content"]  # most recent kept
