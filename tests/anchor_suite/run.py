#!/usr/bin/env python3
"""Anchor pressure-test suite runner.

Fires tasks from the corpus via the Discord webhook, waits for verdicts,
and produces an aggregate report with anchor metrics.

Usage:
    .venv/bin/python tests/anchor_suite/run.py              # full run, print report
    .venv/bin/python tests/anchor_suite/run.py --gate        # exit 1 if thresholds fail
    .venv/bin/python tests/anchor_suite/run.py --quick       # first 5 tasks only
    .venv/bin/python tests/anchor_suite/run.py --category gmail  # specific category
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
load_dotenv()

from tests.anchor_suite.corpus import TASKS

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "state.db"

GATE_THRESHOLDS = {
    "anchor_agreement_rate": float(os.getenv("ANCHOR_AGREEMENT_THRESHOLD", "0.90")),
    "hallucination_rate_max": float(os.getenv("ANCHOR_HALLUCINATION_MAX", "0.05")),
    "coverage_rate": float(os.getenv("ANCHOR_COVERAGE_THRESHOLD", "0.80")),
}


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def fire_webhook(prompt: str) -> bool:
    url = os.getenv("DISCORD_TEST_WEBHOOK_URL", "")
    if not url:
        print("ERROR: DISCORD_TEST_WEBHOOK_URL not set")
        return False
    data = json.dumps({"content": f"!ask {prompt}"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        print(f"  Webhook failed: {e}")
        return False


def wait_for_verdict(baseline_verdict_id: int, timeout: int = 120) -> dict | None:
    conn = get_db()
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        row = conn.execute(
            "SELECT * FROM verdicts WHERE id > ? ORDER BY id ASC LIMIT 1",
            (baseline_verdict_id,),
        ).fetchone()
        if row:
            conn.close()
            return dict(row)
    conn.close()
    return None


def run_tasks(tasks: list[dict]) -> list[dict]:
    results = []
    conn = get_db()

    for i, task in enumerate(tasks):
        baseline = conn.execute("SELECT MAX(id) FROM verdicts").fetchone()[0] or 0
        print(f"\n[{i+1}/{len(tasks)}] {task['id']}: {task['prompt'][:80]}...")

        if not fire_webhook(task["prompt"]):
            results.append({**task, "verdict": None, "error": "webhook_failed"})
            continue

        verdict = wait_for_verdict(baseline, timeout=180)
        if not verdict:
            results.append({**task, "verdict": None, "error": "timeout"})
            print("  TIMEOUT")
            continue

        v = dict(verdict)
        print(f"  verdict={v['verdict']} score={v['score']} anchor_floor={v.get('anchor_floor', 'none')}")

        anchor_reports = []
        if v.get("anchor_reports_json"):
            try:
                anchor_reports = json.loads(v["anchor_reports_json"])
            except json.JSONDecodeError:
                pass

        results.append({
            **task,
            "verdict": v["verdict"],
            "score": v["score"],
            "anchor_floor": v.get("anchor_floor"),
            "anchor_reports": anchor_reports,
            "reasons": json.loads(v["reasons"]) if v.get("reasons") else [],
        })

    conn.close()
    return results


def compute_metrics(results: list[dict]) -> dict:
    completed = [r for r in results if r.get("verdict")]
    if not completed:
        return {"total": 0, "completed": 0}

    anchor_agree = sum(
        1 for r in completed
        if r.get("anchor_floor") is not None and r["anchor_floor"] == r["verdict"]
    )
    anchored = [r for r in completed if r.get("anchor_floor") is not None]

    hallucinated = 0
    for r in completed:
        for reason in r.get("reasons", []):
            if "[ANCHOR FLOOR]" in reason:
                hallucinated += 1
                break

    coverage_explicit = sum(
        1 for r in completed
        if any("retrieved" in str(reason).lower() and any(c.isdigit() for c in str(reason))
               for reason in r.get("reasons", []))
    )

    return {
        "total": len(results),
        "completed": len(completed),
        "correct": sum(1 for r in completed if r["verdict"] == "correct"),
        "degraded": sum(1 for r in completed if r["verdict"] == "degraded"),
        "failed": sum(1 for r in completed if r["verdict"] == "failed"),
        "anchor_agreement_rate": anchor_agree / len(anchored) if anchored else 1.0,
        "hallucination_rate": hallucinated / len(completed) if completed else 0.0,
        "coverage_rate": coverage_explicit / len(completed) if completed else 0.0,
        "timeouts": sum(1 for r in results if r.get("error") == "timeout"),
    }


def print_report(results: list[dict], metrics: dict) -> None:
    print("\n" + "=" * 60)
    print("ANCHOR PRESSURE TEST REPORT")
    print("=" * 60)

    for r in results:
        status = r.get("verdict", r.get("error", "?"))
        floor = r.get("anchor_floor", "-")
        print(f"  {r['id']:30s}  verdict={status:10s}  floor={floor}")

    print(f"\n  Total:    {metrics['total']}")
    print(f"  Completed: {metrics['completed']}")
    print(f"  Correct:  {metrics.get('correct', 0)}")
    print(f"  Degraded: {metrics.get('degraded', 0)}")
    print(f"  Failed:   {metrics.get('failed', 0)}")
    print(f"  Timeouts: {metrics.get('timeouts', 0)}")
    print(f"\n  Anchor agreement rate:  {metrics.get('anchor_agreement_rate', 0):.0%}")
    print(f"  Hallucination rate:     {metrics.get('hallucination_rate', 0):.0%}")
    print(f"  Coverage rate:          {metrics.get('coverage_rate', 0):.0%}")

    print("\n  Gate thresholds:")
    for k, v in GATE_THRESHOLDS.items():
        print(f"    {k}: {v}")


def main():
    parser = argparse.ArgumentParser(description="Anchor pressure-test suite")
    parser.add_argument("--gate", action="store_true", help="Exit 1 if thresholds fail")
    parser.add_argument("--quick", action="store_true", help="Run first 5 tasks only")
    parser.add_argument("--category", type=str, help="Filter by category")
    args = parser.parse_args()

    tasks = TASKS
    if args.category:
        tasks = [t for t in tasks if t["category"] == args.category]
    if args.quick:
        tasks = tasks[:5]

    if not tasks:
        print("No tasks to run.")
        return 0

    print(f"Running {len(tasks)} anchor pressure tests...")
    results = run_tasks(tasks)
    metrics = compute_metrics(results)
    print_report(results, metrics)

    report_path = Path(__file__).parent / "last_report.json"
    with open(report_path, "w") as f:
        json.dump({"results": results, "metrics": metrics}, f, indent=2, default=str)
    print(f"\n  Report saved to {report_path}")

    if args.gate:
        passed = True
        if metrics.get("anchor_agreement_rate", 0) < GATE_THRESHOLDS["anchor_agreement_rate"]:
            print(f"\n  GATE FAIL: anchor_agreement_rate {metrics.get('anchor_agreement_rate', 0):.0%} < {GATE_THRESHOLDS['anchor_agreement_rate']:.0%}")
            passed = False
        if metrics.get("hallucination_rate", 1) > GATE_THRESHOLDS["hallucination_rate_max"]:
            print(f"  GATE FAIL: hallucination_rate {metrics.get('hallucination_rate', 0):.0%} > {GATE_THRESHOLDS['hallucination_rate_max']:.0%}")
            passed = False
        if not passed:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
