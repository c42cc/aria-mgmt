#!/usr/bin/env python3
"""Local chat web-UI acceptance — the good state proven twice (capture + Gemini).

Drives the real browser chat window end-to-end against the LOCAL brain and proves,
two independent ways, that a typed request comes back as a TOOL-BACKED answer:

  1. machine: POST /chat a real request, poll /last until answered, and assert the
     answer is non-empty, not an error, AND tool_fired (a real MCP tool ran — the
     same bar scripts/live_meter.py uses). This is ground truth.
  2. gemini: screenshot the actual browser window (screencapture -l <cgWindowId>,
     the same primitive src/spark.py::display_and_capture uses) and ask Gemini,
     independently, whether the page shows an assistant answer.

A disagreement is a loud FAIL. Halt-don't-heal: if the chat server is down or is
running on cloud (not the local brain), the gate fails with the one-command fix —
it never papers over a missing local brain.

USAGE
  # in one terminal:  make local-chat        (serves the window on the Spark brain)
  # in another:
  .venv/bin/python scripts/local_chat_gate.py

Artifacts: data/spark/local_chat/ (chat.png + local_chat.json).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from src import spark  # noqa: E402
from src.config import config  # noqa: E402

DEFAULT_TASK = (
    "Use the filesystem tool to list your allowed directories, then tell me in one "
    "sentence how many allowed directories there are."
)

_BROWSER_OWNERS = (
    "Google Chrome", "Safari", "Arc", "Brave Browser", "Microsoft Edge",
    "Firefox", "Chromium", "Dia",
)


def _http(method: str, url: str, body: dict | None = None, *, secret: str = "",
          timeout: float = 30.0) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Chat-Secret"] = secret
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _browser_cg_window_id() -> int | None:
    """CoreGraphics window number of the frontmost on-screen browser window."""
    try:
        from Quartz import (  # type: ignore
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
        )
    except Exception:
        return None
    try:
        infos = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
    except Exception:
        return None
    for w in infos or []:
        owner = w.get("kCGWindowOwnerName", "")
        if owner in _BROWSER_OWNERS and int(w.get("kCGWindowLayer", 0)) == 0:
            num = w.get("kCGWindowNumber")
            if num:
                return int(num)
    return None


def _capture_browser(png: Path) -> bool:
    """Screenshot the browser window by CG id; fall back to full screen."""
    cg = _browser_cg_window_id()
    if cg is not None:
        cap = subprocess.run(["screencapture", "-x", "-o", "-l", str(cg), str(png)],
                             capture_output=True, text=True)
        if cap.returncode == 0 and png.exists():
            return True
    cap = subprocess.run(["screencapture", "-x", str(png)], capture_output=True, text=True)
    return cap.returncode == 0 and png.exists()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=config.local_chat_host)
    ap.add_argument("--port", type=int, default=config.local_chat_port)
    ap.add_argument("--secret", default=config.local_chat_secret)
    ap.add_argument("--task", default=DEFAULT_TASK)
    ap.add_argument("--session", default=f"gate{int(time.time())}")
    ap.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for the answer")
    args = ap.parse_args()

    shown_host = "localhost" if args.host in ("0.0.0.0", "127.0.0.1", "::1") else args.host
    base = f"http://{shown_host}:{args.port}"

    # 1. Server up + actually on the LOCAL brain (halt-don't-heal).
    try:
        st, body = _http("GET", base + "/healthz", timeout=8)
    except Exception as e:
        print(f"FATAL: chat server not reachable at {base} ({e}).\n"
              f"  fix: make local-chat   (serve a Spark model first: make spark-serve)", file=sys.stderr)
        return 2
    health = json.loads(body) if st == 200 else {}
    brain = health.get("brain", "")
    print(f"chat health: brain={brain!r} model={health.get('model')!r}", flush=True)
    if not brain or brain == "cloud":
        print("FATAL: the chat is NOT on a local brain (brain='cloud'/unset). This gate "
              "verifies the LOCAL Spark agent; there is no cloud fallback.\n"
              "  fix: ANTHROPIC_BASE_URL=http://<spark-ip>:8000 CLAUDE_MODEL=local-brain make local-chat",
              file=sys.stderr)
        return 2

    # 2. Open the real browser at the gate session, let SSE connect.
    url = f"{base}/?session={args.session}" + (f"&secret={args.secret}" if args.secret else "")
    print(f"opening browser: {url}", flush=True)
    subprocess.run(["open", url], capture_output=True, text=True)
    time.sleep(3.5)

    # 3. Fire a real request through the browser session.
    sc, sb = _http("POST", base + "/chat", {"session": args.session, "message": args.task},
                   secret=args.secret)
    if sc != 200:
        print(f"FATAL: POST /chat returned {sc}: {sb[:200]}", file=sys.stderr)
        return 2

    # 4. Poll /last until the answer renders (deterministic — never a fixed guess).
    deadline = time.monotonic() + args.timeout
    ans: dict | None = None
    while time.monotonic() < deadline:
        lc, lb = _http("GET", base + f"/last?session={args.session}", secret=args.secret)
        d = json.loads(lb) if lc == 200 else {}
        if d.get("answered"):
            ans = d
            break
        time.sleep(2)

    # 5. Screenshot the browser window.
    out_dir = REPO_ROOT / "data" / "spark" / "local_chat"
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / "chat.png"
    time.sleep(1.0)  # let the answer paint before grabbing the window
    captured = _capture_browser(png)

    # 6. Machine assertion (ground truth).
    if ans is None:
        machine_ok, detail = False, f"no answer within {args.timeout:.0f}s"
    elif ans.get("error"):
        machine_ok, detail = False, f"loop errored: {str(ans.get('text'))[:160]}"
    elif not str(ans.get("text", "")).strip():
        machine_ok, detail = False, "empty answer"
    elif not ans.get("tool_fired"):
        machine_ok, detail = False, "answer not tool-backed (no real tool fired)"
    else:
        machine_ok, detail = True, f"tool-backed answer ({len(str(ans.get('text')))} chars)"
    print(f"  machine: {'OK' if machine_ok else 'FAIL'} — {detail}", flush=True)

    # 7. Independent Gemini reading of the real browser screenshot.
    gpass, greason = False, "no screenshot captured"
    if captured:
        try:
            gpass, greason = spark.gemini_verdict(
                png,
                "This is a screenshot of a chat web app. Does it show an assistant reply "
                "to the user's message (an assistant message containing text is visible on the page)?",
            )
        except Exception as e:
            gpass, greason = False, f"gemini error: {e}"
    print(f"  gemini : {'PASS' if gpass else 'FAIL'} — {greason}", flush=True)

    verdict = "PASS" if (machine_ok and gpass) else "FAIL"
    if machine_ok != gpass:
        print("  !! machine/gemini DISAGREEMENT — treating as FAIL", flush=True)

    report = {
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "endpoint": base, "brain": brain, "model": health.get("model"),
        "task": args.task, "verdict": verdict,
        "machine_ok": machine_ok, "machine_detail": detail,
        "gemini_pass": gpass, "gemini_reason": greason,
        "answer_preview": str((ans or {}).get("text", ""))[:800],
        "tool_fired": bool((ans or {}).get("tool_fired")),
        "png": str(png),
    }
    (out_dir / "local_chat.json").write_text(json.dumps(report, indent=2))

    print("\n" + "=" * 60, flush=True)
    print(f"LOCAL CHAT WEB-UI ACCEPTANCE :: {base}", flush=True)
    print(f"  [{verdict}] tool-backed answer rendered in the browser", flush=True)
    print(f"  report: {out_dir / 'local_chat.json'}  png: {png}", flush=True)
    print("=" * 60, flush=True)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
