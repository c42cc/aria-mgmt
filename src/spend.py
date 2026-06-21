"""Spend guard — graceful, never a detonation (review 2.4).

A daily ceiling that degrades predictably: when today's recorded spend reaches
the cap, the next build is held and Aria says so conversationally (the report
turn) instead of erroring mid-flight. It bounds the expensive part (the engine);
the conductor turns are cheap.
"""

from __future__ import annotations

import time

from . import outcome_log
from .config import config


def today_spend_usd() -> float:
    today = time.strftime("%Y-%m-%d")
    return sum(
        float(r.get("cost_usd") or 0.0)
        for r in outcome_log.read_all()
        if str(r.get("ts", "")).startswith(today)
    )


def at_cap() -> bool:
    return today_spend_usd() >= config.daily_spend_cap_usd
