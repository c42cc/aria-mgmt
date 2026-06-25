"""Structural test isolation — the backstop, not per-test discipline.

The 2026-06-25 pollution: the conversation tests instantiate `ConversationBuffer`,
whose write-through (`append_conversation_turn`) lands on `db.DB_PATH`. Tests that
forgot to repoint `DB_PATH` wrote their fixtures ("set up tailscale on spark2",
"request A about emails", an ssh fingerprint) straight into the LIVE
conversation_log — the very telemetry Aria reads for context.

This autouse fixture points `db.DB_PATH` at a throwaway per-test database BEFORE
any test runs, so it is structurally impossible for a unit test to touch the live
DB — regardless of whether the test remembers to isolate itself.
"""

from __future__ import annotations

import pytest

from src import db


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    test_db = str(tmp_path / "state.db")
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()
    yield
