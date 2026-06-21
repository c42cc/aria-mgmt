"""The measurement loop — Aria's review. The one metric that matters.

Reads the outcome log (did real requests deliver) and the experience traces
(per-turn latency), and reports them against the phase's number (review
2.6/3.7). Not a green meter — the honest record you review. `python -m src.review`.
"""

from __future__ import annotations

from collections import defaultdict

from . import conversation, outcome_log


def summarize() -> str:
    rows = outcome_log.read_all()
    out: list[str] = []
    delivered = sum(1 for r in rows if r.get("delivered"))
    out.append(f"Outcomes: {delivered}/{len(rows)} delivered")

    by_loop: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        cell = by_loop[r.get("loop_id") or "(none)"]
        cell[0] += int(bool(r.get("delivered")))
        cell[1] += 1
    for loop_id, (d, n) in sorted(by_loop.items()):
        out.append(f"  {loop_id}: {d}/{n}")

    fails = [r for r in rows if not r.get("delivered")]
    if fails:
        out.append("Not delivered (read these — they're the signal):")
        for r in fails:
            out.append(f"  - {str(r.get('request'))[:55]} :: {r.get('broke')}")

    latencies = conversation.latencies()
    if latencies:
        latencies.sort()
        pick = lambda q: latencies[min(len(latencies) - 1, int(q * len(latencies)))]
        out.append(
            f"Conductor latency ms: p50={pick(0.5)} p95={pick(0.95)} max={max(latencies)} "
            f"(n={len(latencies)}; voice target <~800ms -> tiering, see phase1-voice.md)"
        )

    met = len(rows) >= 10 and delivered >= 9
    out.append(
        "Phase-0 reliability bar (>=9/10 delivered): "
        + ("MET" if met else f"{delivered}/{len(rows)} so far — need 10 real requests")
    )
    return "\n".join(out)


def main() -> int:
    print(summarize())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
