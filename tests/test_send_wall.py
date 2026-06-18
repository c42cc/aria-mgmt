"""Reproduce the iMessage brute-force incident and prove the capability
precheck now closes it MECHANICALLY — the precondition for retiring the
prompt's "do NOT improvise another send path" prose (forensic 2026-06-16:
~10 doomed osascript sends despite the prose rule).

We build a TCC.db with the Messages Automation grant DENIED for python (the
exact failing condition), then assert the precheck blocks the sanctioned send
AND the hand-rolled osascript-via-shell path with the one fix.
"""

from __future__ import annotations

import sqlite3

from src import capability


def _tcc_with(tmp_path, auth_value: int, bundle: str = "com.apple.MobileSMS") -> str:
    dbfile = tmp_path / "TCC.db"
    con = sqlite3.connect(str(dbfile))
    con.execute(
        "CREATE TABLE access (service TEXT, client TEXT, client_type INT, "
        "auth_value INT, indirect_object_identifier TEXT)"
    )
    con.execute(
        "INSERT INTO access VALUES (?,?,?,?,?)",
        ("kTCCServiceAppleEvents", "/opt/homebrew/bin/python3.12",
         1, auth_value, bundle),
    )
    con.commit()
    con.close()
    return str(dbfile)


def test_denied_grant_blocks_send_and_brute_force(tmp_path, monkeypatch):
    monkeypatch.setattr(capability, "_TCC_DB", _tcc_with(tmp_path, auth_value=0))

    # 1. the sanctioned Apple send is blocked before it fires, with the fix
    fix = capability.capability_for("apple", "messages", {"action": "create"}).unmet()
    assert fix and "Automation" in fix

    # 2. the brute-force osascript-via-shell path hits the SAME gate (the prose
    #    rule the model used to ignore is now mechanical)
    osa = {"command": 'osascript -e \'tell application "Messages" to send "x"\''}
    fix2 = capability.capability_for("shell", "execute_command", osa).unmet()
    assert fix2 and "Automation" in fix2

    # 3. an ordinary shell command is unaffected
    assert capability.capability_for("shell", "execute_command", {"command": "ls -la"}).unmet() is None


def test_granted_allows_send(tmp_path, monkeypatch):
    # auth_value 2 == allowed → the precheck must not block.
    monkeypatch.setattr(capability, "_TCC_DB", _tcc_with(tmp_path, auth_value=2))
    assert capability.capability_for("apple", "messages", {"action": "create"}).unmet() is None
