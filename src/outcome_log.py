"""The outcome log — the one measurement that matters.

One append-only JSONL row per real request: what was asked, which loop ran, did
it deliver, what broke. This is the durable signal of quality, reviewed by the
user (review 1.2: keep ONE plain log; the design's verification stack is the
engine's own loop + the loop's done criterion + this log). Not a green meter.
"""

from __future__ import annotations

import json
import time

from .config import config


def record(
    *,
    request: str,
    loop_id: str | None,
    slots: dict | None,
    delivered: bool,
    summary: str,
    broke: str | None = None,
    cost_usd: float | None = None,
    extra: dict | None = None,
) -> dict:
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "request": request,
        "loop_id": loop_id,
        "slots": slots or {},
        "delivered": delivered,
        "summary": summary,
        "broke": broke,
        "cost_usd": cost_usd,
        **(extra or {}),
    }
    config.outcome_log_path.parent.mkdir(parents=True, exist_ok=True)
    with config.outcome_log_path.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def read_all() -> list[dict]:
    p = config.outcome_log_path
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
