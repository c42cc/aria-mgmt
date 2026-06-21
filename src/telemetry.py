"""Experience telemetry — so we can debug FEEL, not just correctness.

An audit log and a pass/fail outcome log cannot tell you why a session felt
janky (review 2.6). This captures, from day one, per-turn timing and the full
conversation trace: where latency went, what Aria asked and why, where the user
repeated themselves, where a turn got cut off. One JSON file per session.

`latency_ms` on an Aria turn is the conductor's think+respond time — the text
proxy for the voice metric that decides whether it feels alive (review 2.2).
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

from .config import config


@dataclass
class Trace:
    session_id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    started_at: float = field(default_factory=time.time)
    turns: list[dict] = field(default_factory=list)

    def _add(self, **row) -> None:
        row["t_rel_ms"] = round((time.time() - self.started_at) * 1000)
        self.turns.append(row)
        self.save()

    def user(self, text: str) -> None:
        self._add(role="user", text=text)

    def aria(self, text: str, phase: str, latency_ms: int, loop_id: str | None = None) -> None:
        self._add(role="aria", text=text, phase=phase, latency_ms=latency_ms, loop_id=loop_id)

    def observation(self, text: str) -> None:
        self._add(role="observation", text=text)

    @contextmanager
    def time_turn(self):
        """Measure an Aria turn's think+respond latency."""
        start = time.time()
        box: dict[str, int] = {}
        yield box
        box["latency_ms"] = round((time.time() - start) * 1000)

    def latencies(self) -> list[int]:
        return [t["latency_ms"] for t in self.turns if t.get("role") == "aria" and "latency_ms" in t]

    def summary(self) -> dict:
        lat = sorted(self.latencies())
        p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else None
        return {
            "session_id": self.session_id,
            "aria_turns": len(self.latencies()),
            "latency_ms_p50": p(0.5),
            "latency_ms_p95": p(0.95),
            "latency_ms_max": max(lat) if lat else None,
        }

    def save(self) -> None:
        config.trace_dir.mkdir(parents=True, exist_ok=True)
        path = config.trace_dir / f"{self.session_id}.json"
        path.write_text(json.dumps({
            "session_id": self.session_id,
            "started_at": self.started_at,
            "summary": self.summary(),
            "turns": self.turns,
        }, indent=2))
