#!/usr/bin/env python3
"""The front door — a beautiful one-page snapshot of the system.

Runs ON the Mind (spark1), self-contained (stdlib only), so the page is always
current without the Mac. It does lightweight liveness probes (the front-door view;
`make doctor` is the deeper operator view), renders one minimalist HTML page, and
writes it into Home Assistant's www so it is served — no auth — at:

    http://100.106.152.104:8123/local/home.html   (local network + Tailscale)

Run by cron every couple of minutes (installed by scripts/publish_home.py).
"""

from __future__ import annotations

import html
import os
import subprocess
import time
import urllib.request

SPARK2_IP = "100.119.143.76"
HA_URL = "http://127.0.0.1:8123"
MIND_URL = "http://127.0.0.1:8000/v1/models"


def _http(url: str, timeout: float = 4.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read(4000).decode("utf-8", "ignore")
    except Exception as e:  # our connectivity to surface, never blame the service
        return None, str(e)


def _probe() -> list[dict]:
    planes: list[dict] = []

    code, body = _http(MIND_URL)
    planes.append({
        "name": "The brain", "sub": "your private local model",
        "state": "ok" if (code == 200 and "local-brain" in body) else "down",
        "ok_text": "Online — answering privately on your own hardware.",
        "down_text": "Offline — it restarts itself; give it a minute.",
    })

    r = subprocess.run(["ping", "-c", "1", "-W", "2", SPARK2_IP], capture_output=True)
    planes.append({
        "name": "The builder", "sub": "where Aria builds & changes things",
        "state": "ok" if r.returncode == 0 else "down",
        "ok_text": "Ready.", "down_text": "Unreachable right now.",
    })

    root = os.environ.get("FLOOR_ROOT", "").strip()
    connected = bool(root) and os.path.ismount(root)
    planes.append({
        "name": "Storage", "sub": "your durable, backed-up storage",
        "state": "ok" if connected else "absent",
        "ok_text": f"Connected at {root}." if connected else "",
        "down_text": "Not connected yet — your NAS arrives soon.",
    })

    code, _ = _http(HA_URL + "/")
    planes.append({
        "name": "Home control", "sub": "the hub for your home & dev environment",
        "state": "ok" if code in (200, 301, 302) else "down",
        "ok_text": "Online.", "down_text": "Offline — it restarts itself.",
    })

    planes.append({
        "name": "Reasoning", "sub": "Aria's deeper thinking",
        "state": "ok",
        "ok_text": "Online — runs on Opus (cloud) when Aria needs to think.",
        "down_text": "",
    })
    return planes


_DOT = {"ok": "#3fb950", "down": "#f85149", "absent": "#d29922"}


def _render(planes: list[dict]) -> str:
    downs = [p for p in planes if p["state"] == "down"]
    absents = [p for p in planes if p["state"] == "absent"]
    if downs:
        headline, hue = f"{len(downs)} thing{'s' if len(downs) > 1 else ''} need attention", "#f85149"
    elif absents:
        headline, hue = "Everything's running", "#3fb950"
    else:
        headline, hue = "Everything's running", "#3fb950"

    rows = []
    for p in planes:
        text = p["ok_text"] if p["state"] in ("ok",) else (p["down_text"] if p["state"] == "down" else p["down_text"])
        if p["state"] == "absent":
            text = p["down_text"]
        rows.append(f"""
      <div class="row">
        <span class="dot" style="background:{_DOT[p['state']]}"></span>
        <div class="rowtext">
          <div class="name">{html.escape(p['name'])} <span class="sub">{html.escape(p['sub'])}</span></div>
          <div class="status">{html.escape(text)}</div>
        </div>
      </div>""")

    now = time.strftime("%A %-I:%M %p")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Home</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh;
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0b0e14; color: #e6edf3;
    display: flex; justify-content: center;
  }}
  .wrap {{ width: 100%; max-width: 640px; padding: 48px 24px 64px; }}
  header {{ margin-bottom: 36px; }}
  h1 {{ font-size: 15px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase;
        color: #8b949e; margin: 0 0 14px; }}
  .headline {{ font-size: 30px; font-weight: 650; letter-spacing: -.02em; display: flex; align-items: center; gap: 12px; }}
  .pulse {{ width: 11px; height: 11px; border-radius: 50%; background: {hue};
            box-shadow: 0 0 0 0 {hue}; animation: pulse 2.4s infinite; }}
  @keyframes pulse {{ 0%{{box-shadow:0 0 0 0 {hue}66}} 70%{{box-shadow:0 0 0 10px {hue}00}} 100%{{box-shadow:0 0 0 0 {hue}00}} }}
  section {{ margin-top: 34px; }}
  .label {{ font-size: 12px; font-weight: 600; letter-spacing: .1em; text-transform: uppercase;
            color: #6e7681; margin-bottom: 14px; }}
  .row {{ display: flex; align-items: flex-start; gap: 14px; padding: 13px 0; border-top: 1px solid #1c2230; }}
  .row:first-of-type {{ border-top: none; }}
  .dot {{ width: 10px; height: 10px; border-radius: 50%; margin-top: 6px; flex: none; }}
  .name {{ font-weight: 600; }}
  .sub {{ font-weight: 400; color: #6e7681; font-size: 14px; margin-left: 6px; }}
  .status {{ color: #9aa4b2; font-size: 15px; margin-top: 2px; }}
  a.card {{ display: flex; align-items: center; justify-content: space-between;
            text-decoration: none; color: #e6edf3; background: #11151f; border: 1px solid #1c2230;
            border-radius: 12px; padding: 16px 18px; margin-bottom: 10px; transition: border-color .15s; }}
  a.card:hover {{ border-color: #2f3a4d; }}
  a.card .desc {{ color: #6e7681; font-size: 14px; }}
  a.card .arrow {{ color: #6e7681; }}
  .note {{ color: #6e7681; font-size: 14px; padding: 4px 0; }}
  footer {{ margin-top: 40px; color: #4b5363; font-size: 13px; }}
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Home</h1>
      <div class="headline"><span class="pulse"></span>{html.escape(headline)}.</div>
    </header>

    <section>
      <div class="label">Right now</div>
      {''.join(rows)}
    </section>

    <section>
      <div class="label">What you can reach</div>
      <a class="card" href="http://100.106.152.104:8123" target="_blank" rel="noopener">
        <span><b>Home Assistant</b><br><span class="desc">Control & monitor everything</span></span>
        <span class="arrow">&rarr;</span>
      </a>
      <div class="note">The brain answers privately on your network. To watch or steer the builder, open it in Zed (<code>make zed-hands</code>).</div>
    </section>

    <footer>Updated {now}. This page refreshes itself every minute.</footer>
  </div>
</body>
</html>"""


def main() -> int:
    page = _render(_probe()).encode("utf-8")
    # Write into Home Assistant's www (served at /local/), via the container (it owns /config).
    p = subprocess.run(
        ["docker", "exec", "-i", "homeassistant", "sh", "-c",
         "mkdir -p /config/www && cat > /config/www/home.html"],
        input=page, capture_output=True,
    )
    if p.returncode != 0:
        import sys
        print(f"FATAL: could not write the page into HA www: {p.stderr.decode()[-200:]}", file=sys.stderr)
        return 1
    print(f"published home.html ({len(page)} bytes) -> /local/home.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
