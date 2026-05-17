#!/usr/bin/env python3
"""Verify Full Disk Access by reading the macOS Mail envelope index.

Exits 0 if mail DB is readable (FDA granted), 1 otherwise.
Run with the same Python that runs the bot:
    .venv/bin/python scripts/verify_mail_access.py
"""

import glob
import os
import sqlite3
import sys


def main() -> int:
    mail_dir = os.path.expanduser("~/Library/Mail")
    if not os.access(mail_dir, os.R_OK):
        print(f"FAIL: Cannot read {mail_dir} — Full Disk Access not granted.")
        print()
        print("Fix: System Settings > Privacy & Security > Full Disk Access")
        print(f"     Add: {sys.executable}")
        print("     Also add: /opt/homebrew/bin/node")
        return 1

    pattern = os.path.join(mail_dir, "V*/MailData/Envelope Index")
    paths = sorted(glob.glob(pattern))

    if not paths:
        print(f"FAIL: No Mail DB found matching {pattern}")
        print("      Full Disk Access is granted, but Apple Mail may not be configured.")
        return 1

    db_path = paths[-1]
    print(f"Found mail DB: {db_path}")

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.close()
    except sqlite3.OperationalError as e:
        if "unable to open" in str(e).lower() or "readonly" in str(e).lower():
            print(f"FAIL: Cannot open mail DB — Full Disk Access not granted.")
            print(f"      Error: {e}")
            print()
            print("Fix: System Settings > Privacy & Security > Full Disk Access")
            print(f"     Add: {sys.executable}")
            print("     Also add: /opt/homebrew/bin/node")
            return 1
        raise
    except PermissionError as e:
        print(f"FAIL: Permission denied — Full Disk Access not granted.")
        print(f"      Error: {e}")
        return 1

    print(f"OK: Full Disk Access confirmed — {count} messages in mail index.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
