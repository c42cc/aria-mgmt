#!/usr/bin/env python3
"""Deploy the front-door page to the Mind and keep it fresh — idempotent.

Pushes ops/home/refresh_home_page.py to spark1, runs it once, and installs a cron
so the page self-refreshes every 2 minutes (no Mac dependency). After this the
front door is live (no auth) on the local network and over Tailscale at:

    http://100.106.152.104:8123/local/home.html

    .venv/bin/python scripts/publish_home.py      # or: make home
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NODE = "spark1"
REMOTE = "ops/home/refresh_home_page.py"
URL = "http://100.106.152.104:8123/local/home.html"


def _ssh(cmd: str, **kw):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", NODE, cmd], **kw)


def main() -> int:
    local = REPO / REMOTE
    if not local.exists():
        print(f"FATAL: missing {local}", file=sys.stderr)
        return 2

    with open(local, "rb") as f:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", NODE,
             f"mkdir -p ~/ops/home && cat > ~/{REMOTE}"],
            stdin=f, capture_output=True, text=True,
        )
    if p.returncode:
        print(f"FATAL: deploy failed: {p.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"deployed refresher -> {NODE}:~/{REMOTE}")

    p = _ssh(f"python3 ~/{REMOTE}", capture_output=True, text=True)
    print((p.stdout or p.stderr).strip())
    if p.returncode:
        print("FATAL: first publish failed", file=sys.stderr)
        return 1

    # Hands-free freshness: cron every 2 minutes (dedup any prior entry).
    cron = (f'(crontab -l 2>/dev/null | grep -v refresh_home_page; '
            f'echo "*/2 * * * * /usr/bin/python3 $HOME/{REMOTE} >/dev/null 2>&1") | crontab -')
    p = _ssh(cron, capture_output=True, text=True)
    if p.returncode:
        print(f"WARN: could not install cron ({p.stderr.strip()}); page still refreshes when this runs.", file=sys.stderr)
    else:
        print("cron installed on the Mind: the page self-refreshes every 2 minutes")

    print(f"\nFront door is live: {URL}")
    print("  - local network: open it directly")
    print("  - remote: same URL over Tailscale (HA is on the tailnet) — deferred for a public/no-VPN path")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
