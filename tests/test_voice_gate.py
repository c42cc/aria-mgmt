"""Gemini Live realtime-input gate (forensic 2026-06-16).

Gemini Live closes the socket with 1008 if any send_realtime_input (audio or
audio_stream_end) arrives between a tool_call and its send_tool_response. These
tests prove the gate suppresses realtime input while a tool call is pending and
reopens it when the dispatch finishes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from src.gemini_session import GeminiSession


def _session_with_mock() -> GeminiSession:
    gs = GeminiSession()
    gs._connected = True
    gs._session = AsyncMock()
    return gs


def test_send_audio_suppressed_while_tool_pending():
    gs = _session_with_mock()
    gs._pending_tool_calls = 1
    asyncio.run(gs.send_audio(b"\x00\x01" * 160))
    gs._session.send_realtime_input.assert_not_called()


def test_send_audio_flows_when_no_tool_pending():
    gs = _session_with_mock()
    gs._pending_tool_calls = 0
    asyncio.run(gs.send_audio(b"\x00\x01" * 160))
    gs._session.send_realtime_input.assert_awaited_once()


def test_audio_stream_end_suppressed_while_tool_pending():
    gs = _session_with_mock()
    gs._pending_tool_calls = 2
    asyncio.run(gs.signal_audio_end())
    gs._session.send_realtime_input.assert_not_called()


def test_audio_stream_end_flows_when_idle():
    gs = _session_with_mock()
    gs._pending_tool_calls = 0
    asyncio.run(gs.signal_audio_end())
    gs._session.send_realtime_input.assert_awaited_once()


def test_on_dispatch_done_reopens_gate_and_never_negative():
    gs = _session_with_mock()
    gs._pending_tool_calls = 2
    gs._on_dispatch_done(None)
    assert gs._pending_tool_calls == 1
    gs._on_dispatch_done(None)
    assert gs._pending_tool_calls == 0
    gs._on_dispatch_done(None)  # never underflows
    assert gs._pending_tool_calls == 0
