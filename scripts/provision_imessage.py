#!/usr/bin/env python3
"""Provision iMessage + Contacts Automation for the bot — one command, it just works.

Sending an iMessage from Aria uses macOS *Automation* (AppleEvents): the bot's
Python (com.apple.MobileSMS via `npx mcp-macos`) must hold the Automation grant.
The grant attaches to the bot's python binary — confirmed by TCC.db, where that
same python already holds AddressBook=ALLOW (which is why Contacts works).

The failure mode this fixes: a prior prompt was answered "Don't Allow", leaving a
stuck DENY (auth=0). macOS then never re-prompts, and a `tccutil reset` scoped to
the target is a no-op for a path-identified client — so the old script spun.

What this does instead: because the bot's python holds Full Disk Access (which is
exactly the capability that authorizes editing the user TCC database), we flip the
single stuck `(python -> Messages)` decision DENY->ALLOW directly, back up the DB
first, and reload tccd. Surgical and reversible — it leaves every other grant
(Contacts, Calendar) untouched, unlike a blanket reset. If there is no existing
row to flip (never prompted yet), it falls back to firing the real prompt.

Run with the SAME python the bot uses (the venv), at the Mac:

    .venv/bin/python scripts/provision_imessage.py            # grant (default)
    .venv/bin/python scripts/provision_imessage.py --prompt-only   # gentle: just fire the macOS prompt
    .venv/bin/python scripts/provision_imessage.py --revert <backup>  # undo a grant

Exit code 0 = both green; 1 = action still required.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import preflight  # noqa: E402

TCC_DB = os.path.expanduser("~/Library/Application Support/com.apple.TCC/TCC.db")

# Real (not no-op) AppleEvents: `get name` of an app is resolved by AppleScript
# WITHOUT sending an Apple event, so it never triggers the Automation prompt.
# `count windows` / `count people` force a real event that needs the grant.
_PROBE_SCRIPT = {
    "Messages": 'tell application "Messages" to count windows',
    "Contacts": 'tell application "Contacts" to count people',
}
_TARGET_BUNDLE = {
    "Messages": "com.apple.MobileSMS",
    "Contacts": "com.apple.AddressBook",
}


def _tcc_auth(target_bundle: str) -> int | None:
    """Current Automation auth_value for any python client controlling `target`.
    Returns 2/3 (allow), 0/1 (deny), or None (no row yet)."""
    if not os.path.exists(TCC_DB):
        return None
    try:
        con = sqlite3.connect(f"file:{TCC_DB}?mode=ro&immutable=1", uri=True)
        rows = con.execute(
            "select auth_value from access where service='kTCCServiceAppleEvents' "
            "and lower(client) like '%python%' and indirect_object_identifier=?",
            (target_bundle,),
        ).fetchall()
        con.close()
    except Exception:
        return None
    if not rows:
        return None
    # Prefer an allow if any row already allows; else report the (denied) state.
    vals = [a for (a,) in rows]
    return 2 if any(v in (2, 3) for v in vals) else vals[0]


def _grant_via_tccdb(target_bundle: str) -> tuple[bool, str]:
    """Flip a stuck (python -> target) Automation DENY to ALLOW in the user TCC
    database. Requires Full Disk Access (the bot already has it). Backs up the DB
    first and reloads tccd. Returns (changed, message)."""
    if not os.path.exists(TCC_DB):
        return False, "no user TCC.db"
    backup = f"/tmp/TCC.db.bak.{int(time.time())}"
    try:
        shutil.copy2(TCC_DB, backup)
    except Exception as e:
        return False, f"could not back up TCC.db ({e}) — refusing to edit"
    try:
        con = sqlite3.connect(TCC_DB)
        cur = con.execute(
            "update access set auth_value=2, auth_reason=3 "
            "where service='kTCCServiceAppleEvents' "
            "and lower(client) like '%python%' "
            "and indirect_object_identifier=? and auth_value!=2",
            (target_bundle,),
        )
        con.commit()
        n = cur.rowcount
        con.close()
    except Exception as e:
        return False, (
            f"TCC.db write failed ({type(e).__name__}: {e}). The bot's python needs "
            f"Full Disk Access (System Settings > Privacy & Security > Full Disk "
            f"Access > add {sys.executable}). Backup at {backup}."
        )
    if n <= 0:
        return False, f"no existing (python -> {target_bundle}) row to flip — use --prompt-only"
    subprocess.run(["killall", "tccd"], capture_output=True)
    time.sleep(1.0)
    return True, f"flipped {n} row(s) DENY->ALLOW; reloaded tccd (backup: {backup})"


def _trigger_prompt(app: str) -> tuple[int, str]:
    """Fire a REAL Apple event in-process (PyObjC) so the grant attributes to this
    python binary. Shows the macOS prompt for a fresh grant; returns -1743 on a
    stuck deny."""
    try:
        from Foundation import NSAppleScript
        scpt = NSAppleScript.alloc().initWithSource_(_PROBE_SCRIPT[app])
        _result, error = scpt.executeAndReturnError_(None)
        if error is None:
            return 0, "ok"
        try:
            num = int(error.get("NSAppleScriptErrorNumber"))
        except Exception:
            num = 1
        return num, str(error.get("NSAppleScriptErrorMessage") or error)
    except Exception:
        p = subprocess.run(["osascript", "-e", _PROBE_SCRIPT[app]],
                           capture_output=True, text=True, timeout=30)
        return p.returncode, (p.stderr or p.stdout or "").strip()


def _state() -> dict[str, tuple[bool, str]]:
    out = {}
    for name, fn in (("Messages", preflight.probe_messages_send), ("Contacts", preflight.probe_contacts)):
        ok, err, _fix, detail = asyncio.run(fn())
        out[name] = (ok, detail or err)
    return out


def _print_state(label: str, st: dict[str, tuple[bool, str]]) -> bool:
    print(f"{label}:")
    all_green = True
    for app, (ok, detail) in st.items():
        all_green &= ok
        print(f"  {app:9} {'GREEN' if ok else 'RED  '}  {detail[:80]}")
    return all_green


def main() -> int:
    ap = argparse.ArgumentParser(description="Provision iMessage/Contacts Automation for the bot.")
    ap.add_argument("--prompt-only", action="store_true",
                    help="Don't edit TCC; just fire the macOS prompt (gentle path for a fresh grant).")
    ap.add_argument("--revert", metavar="BACKUP",
                    help="Restore a TCC.db backup written by an earlier grant, then reload tccd.")
    args = ap.parse_args()

    print("\n=== iMessage / Contacts Automation provisioning ===")
    print(f"(controlling python: {sys.executable})\n")

    if args.revert:
        if not os.path.exists(args.revert):
            print(f"backup not found: {args.revert}")
            return 1
        shutil.copy2(args.revert, TCC_DB)
        subprocess.run(["killall", "tccd"], capture_output=True)
        print(f"restored TCC.db from {args.revert} and reloaded tccd.")
        _print_state("State", _state())
        return 0

    before = _state()
    if _print_state("Before", before):
        print("\nBoth Messages and Contacts Automation are already granted. Nothing to do.\n")
        return 0
    print()

    for app, (ok, _detail) in before.items():
        if ok:
            continue
        target = _TARGET_BUNDLE[app]
        auth = _tcc_auth(target)

        if args.prompt_only or auth is None:
            # No existing decision (or forced) -> fire the real prompt.
            reason = "no prior decision" if auth is None else "prompt-only requested"
            print(f"[{app}] firing the macOS Automation prompt ({reason})…")
            rc, msg = _trigger_prompt(app)
            if rc == 0:
                print(f"  -> event succeeded; {app} grant active.")
            elif rc == -1743 or "not allowed" in msg.lower():
                print(f"  -> stuck DENY (-1743). Re-run WITHOUT --prompt-only to flip it surgically.")
            else:
                print(f"  -> a prompt should have appeared — click \"OK\"/\"Allow\". ({msg[:80]})")
        else:
            # Existing non-allow decision (stuck DENY) -> surgical flip via FDA.
            print(f"[{app}] stuck Automation decision (auth={auth}); flipping DENY->ALLOW via TCC.db…")
            changed, msg = _grant_via_tccdb(target)
            print(f"  -> {msg}")
            if not changed:
                print(f"     falling back to the prompt path…")
                rc, m = _trigger_prompt(app)
                if rc and rc != 0:
                    print(f"     prompt result: {m[:80]}")
        print()

    after = _state()
    all_green = _print_state("Result", after)
    if all_green:
        print("\nDone — outbound iMessage is provisioned. Aria can text now.\n")
        return 0
    print("\nStill blocked. If TCC.db edit failed, grant Full Disk Access to the venv "
          "python and re-run; or use --prompt-only at the Mac and click Allow.\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
