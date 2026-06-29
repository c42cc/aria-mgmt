"""Source-of-truth voice-activity log — when Aria actually FIRED.

One append-only JSONL (`data/voice_activity.jsonl`) so "when did she last talk?"
is a `tail`, not a grep through a 100 MB stderr.log full of Discord gateway
noise. Every voice-relevant moment lands here with a timestamp:

  wake      — a wake word opened a session (carries the score)
  heard     — a transcribed user turn (what you said)
  spoke     — Aria produced a spoken turn (THIS is "she fired"), with the text
  go_away   — she was told to go quiet (carries seconds + how: voice/text)
  came_back — the away state was cleared / expired
  session   — a voice session opened/closed (carries state)

Stdlib only; safe to call from anywhere (failures shout to stderr, never crash
the voice path).
"""

from __future__ import annotations

import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(REPO_ROOT, "data", "voice_activity.jsonl")


def log(event: str, **fields) -> None:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
             "mono": round(time.monotonic(), 3), "event": event, **fields}
    try:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        sys.stderr.write(f"[voice_activity] ledger write failed: {exc} :: {entry}\n")


def tail(n: int = 20) -> list[dict]:
    try:
        with open(LEDGER, encoding="utf-8") as fh:
            lines = fh.readlines()[-n:]
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def last_fired() -> dict | None:
    """The most recent 'spoke' event — the last time Aria actually talked."""
    try:
        with open(LEDGER, encoding="utf-8") as fh:
            for ln in reversed(fh.readlines()):
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e.get("event") == "spoke":
                    return e
    except FileNotFoundError:
        return None
    return None


def _main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "last":
        e = last_fired()
        print(json.dumps(e) if e else "no 'spoke' events recorded yet")
        return 0
    for e in tail(int(argv[2]) if len(argv) >= 3 else 30):
        line = f"{e.get('ts','?')}  {e.get('event','?'):9}"
        for k, v in e.items():
            if k in ("ts", "mono", "event"):
                continue
            line += f"  {k}={v}"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
