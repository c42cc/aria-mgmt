"""Aria's presence / "go away" primitive — one durable home for going quiet.

A single `away_until` epoch in `data/presence.json`. Her voice paths check it:
the wake-word listener, voice auto-join, and proactive speech all fall silent
while `now < away_until`, then auto-resume when it passes. No process kill, no
launchd respawn, no reboot self-talk (the 3 AM false-wake that started this).

Voice-only by design: phone notifications are NOT gated here — "go away" quiets
her presence, it does not blind the notify path. One value, read at the gates.
"""

from __future__ import annotations

import json
import os
import re
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(REPO_ROOT, "data", "presence.json")


def _read_until() -> float:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return float(json.load(fh).get("away_until", 0.0))
    except (OSError, ValueError):
        return 0.0


def _write_until(until: float) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"away_until": until}, fh)
    os.replace(tmp, STATE)


def is_away(now: float | None = None) -> bool:
    return (now if now is not None else time.time()) < _read_until()


def remaining(now: float | None = None) -> float:
    return max(0.0, _read_until() - (now if now is not None else time.time()))


def set_away(seconds: float) -> float:
    """Go quiet for `seconds`. Returns the absolute resume time."""
    until = time.time() + max(0.0, seconds)
    _write_until(until)
    return until


def clear_away() -> None:
    """Come back now."""
    _write_until(0.0)


def describe(now: float | None = None) -> str:
    rem = remaining(now)
    if rem <= 0:
        return "present"
    mins = int((rem + 59) // 60)  # round up to the next minute for display
    if mins < 60:
        return f"away ~{mins}m"
    return f"away ~{mins // 60}h{mins % 60:02d}m"


_DUR = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*"
    r"(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)?\s*$",
    re.IGNORECASE,
)


def parse_duration(text: str) -> float | None:
    """Parse '30m', '30 minutes', '9h', '90s', or a bare number (= minutes) to
    seconds. Returns None if it isn't a duration."""
    if not text:
        return None
    m = _DUR.match(text)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "m").lower()
    if unit.startswith("s"):
        return val
    if unit.startswith("h"):
        return val * 3600.0
    return val * 60.0
