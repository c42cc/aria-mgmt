"""Tests for the Capability primitive (src/capability.py).

These reproduce the actual 72h failures and watch them flip:
  - `screencapture: command not found` while the binary sat in /usr/sbin,
  - a hand-rolled / sanctioned Apple send firing into a missing Automation
    grant instead of failing clean with the one fix.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from src.capability import (
    Capability,
    Precond,
    automation_grant,
    binary,
    capability_for,
    contracted_env,
    contracted_path,
    requires_for,
    unverified_world_changes,
)


def test_contracted_path_adds_standard_dirs():
    p = contracted_path("/usr/bin:/bin")
    parts = p.split(os.pathsep)
    # original entries preserved, in order, first
    assert parts[0] == "/usr/bin" and parts[1] == "/bin"
    # guaranteed dirs that exist are appended
    if os.path.isdir("/usr/sbin"):
        assert "/usr/sbin" in parts
    # idempotent: re-normalizing doesn't duplicate
    assert contracted_path(p) == p


@pytest.mark.skipif(
    not os.path.exists("/usr/sbin/screencapture"),
    reason="screencapture only exists on macOS",
)
def test_reproduce_screencapture_command_not_found_flip():
    # The original failing condition: a stripped PATH cannot find screencapture.
    stripped = "/usr/bin:/bin"
    assert shutil.which("screencapture", path=stripped) is None  # the bug
    # The contract flips it green — the binary was always there.
    fixed = contracted_path(stripped)
    assert shutil.which("screencapture", path=fixed) is not None


def test_contracted_env_preserves_vars_and_normalizes_path():
    env = contracted_env(base={"PATH": "/usr/bin", "FOO": "bar"})
    assert env["FOO"] == "bar"
    assert "/usr/bin" in env["PATH"].split(os.pathsep)
    if os.path.isdir("/usr/sbin"):
        assert "/usr/sbin" in env["PATH"].split(os.pathsep)


def test_contracted_env_overlay_wins():
    env = contracted_env(base={"PATH": "/usr/bin"}, extra={"TOKEN": "xyz"})
    assert env["TOKEN"] == "xyz"


def test_binary_precond_found_and_missing():
    ok, fix = binary("ls").check()
    assert ok and fix == ""
    ok, fix = binary("definitely_missing_binary_zzz").check()
    assert not ok
    assert "definitely_missing_binary_zzz" in fix


def test_capability_unmet_returns_first_fix():
    bad = Precond("bad", lambda: (False, "DO THE FIX"))
    good = Precond("good", lambda: (True, ""))
    assert Capability("x", requires=(good, bad)).unmet() == "DO THE FIX"
    assert Capability("x", requires=(good,)).unmet() is None
    assert Capability("x").unmet() is None  # no requires => always met


def test_confirm_postcondition_semantics():
    # verify defined and passing
    cap = Capability("x", verify=lambda r: (True, "saw it"))
    assert cap.confirm("result") == (True, "saw it")
    # verify defined and failing
    cap = Capability("x", verify=lambda r: (False, "not delivered"))
    assert cap.confirm("result") == (False, "not delivered")
    # NO verify => unverified by design (never a silent pass)
    assert Capability("x").confirm("result") == (False, "")
    # a broken verify must not pass as success
    def boom(_r):
        raise RuntimeError("kaboom")
    verified, note = Capability("x", verify=boom).confirm("r")
    assert verified is False and "kaboom" in note


def test_requires_for_routing():
    # apple send + contact lookup carry the matching Automation grant
    assert any(
        p.label == "automation:Messages"
        for p in requires_for("apple", "messages", {"action": "create"})
    )
    assert any(
        p.label == "automation:Contacts"
        for p in requires_for("apple", "contacts", {"name": "Mom"})
    )
    # unconditioned tools carry nothing
    assert requires_for("filesystem", "read_text_file", {"path": "/x"}) == ()
    assert requires_for("gmail", "search_emails", {"query": "x"}) == ()


def test_shell_osascript_send_backstop():
    # A raw osascript Apple-events send hits the SAME Messages wall — gate it.
    cmd = {"command": 'osascript -e \'tell application "Messages" to send "hi"\''}
    preconds = requires_for("shell", "execute_command", cmd)
    assert any(p.label == "automation:Messages" for p in preconds)
    # An ordinary shell command is unconditioned.
    assert requires_for("shell", "execute_command", {"command": "ls -la"}) == ()


@pytest.mark.skipif(sys.platform != "darwin", reason="TCC is macOS-only")
def test_automation_grant_never_blocks_on_uncertainty():
    # Whatever this machine's grant state, the check must return a (bool, str)
    # and must never raise — uncertainty resolves to allow, not block.
    ok, fix = automation_grant("Messages").check()
    assert isinstance(ok, bool) and isinstance(fix, str)


def test_capability_for_builds_named_capability():
    cap = capability_for("apple", "messages", {"action": "create"})
    assert cap.name == "apple.messages"
    assert isinstance(cap.unmet(), (str, type(None)))


def test_unverified_flags_failed_world_change():
    trace = [
        {"tool": "messages_create", "result": "not authorized to send Apple events (-1743)"},
        {"tool": "search_emails", "result": "found 3 emails"},  # read: never flagged
    ]
    assert unverified_world_changes(trace) == ["messages_create"]


def test_unverified_ignores_successful_send_and_reads():
    trace = [
        {"tool": "send_email", "result": "Message sent, id=abc123"},
        {"tool": "read_text_file", "result": "permission denied"},  # a read, not world-changing
    ]
    assert unverified_world_changes(trace) == []


def test_unverified_catches_typed_error_envelope():
    trace = [{"tool": "create_42c_account",
              "result": '{"_error_class": "permission", "error": "nope"}'}]
    assert unverified_world_changes(trace) == ["create_42c_account"]


def test_unverified_empty_and_none():
    assert unverified_world_changes([]) == []
    assert unverified_world_changes(None) == []


def test_unverified_flags_apple_send_failure_real_shape():
    # The exact failure shape from the audit log.
    trace = [{
        "tool": "messages_chat",
        "args": {"action": "create", "chatId": "x"},
        "result": "[TextContent(text='Failed to send message: Messages did not respond in time.')]",
    }]
    assert unverified_world_changes(trace) == ["messages_chat"]


def test_unverified_ignores_apple_read():
    trace = [{
        "tool": "messages_chat",
        "args": {"action": "read", "dateRange": "today"},
        "result": "### Chats (Total: 3) ...",
    }]
    assert unverified_world_changes(trace) == []


def test_unverified_flags_send_email_without_confirmation():
    trace = [{"tool": "send_email", "args": {"to": "x@y.com"},
              "result": "(no clear confirmation returned)"}]
    assert unverified_world_changes(trace) == ["send_email"]


def test_unverified_passes_send_email_with_confirmation():
    trace = [{"tool": "send_email", "args": {"to": "x@y.com"},
              "result": "Email sent successfully. Message ID: 19ed20bee33d8ca2"}]
    assert unverified_world_changes(trace) == []
