#!/usr/bin/env python3
"""DGX Spark — Section A acceptance harness (capture + Gemini visual verify).

Thin CLI over `src/spark.py` — all the logic (gate catalog, SSH ground truth,
macOS-Terminal capture, Gemini visual verdict, setup) lives in that one module so
the CLI and Aria's spark tools share a single implementation. For each runbook
"good state" the shared `verify()`:

  1. Runs the probe over SSH (ground truth: stdout + exit code), then replays it
     LIVE in a real macOS Terminal window so the remote output is literally on
     screen, and screenshots that window.
  2. Asks Gemini to independently confirm the screenshot shows the success
     condition (temperature 0).

A gate PASSES only if the machine assertion AND the Gemini verdict agree. Any
failure -- or any disagreement between the two -- is loud: the gate is marked
FAIL with the runbook fix command, and the harness exits non-zero. No silent
fallbacks. Artifacts land in data/spark/<node>/: one PNG per gate plus
acceptance.json.

USAGE
  .venv/bin/python scripts/spark_acceptance.py --node spark1 --role A
  .venv/bin/python scripts/spark_acceptance.py --node spark1 --role A --run-setup
  .venv/bin/python scripts/spark_acceptance.py --node spark1 --role A --only claude_auth,gpu

Secrets: ANTHROPIC_API_KEY is read from .env only to seed the node and to run
the auth round-trip; it is never printed or screenshotted (the displayed
command shows `$(cat ...)`, never the value).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import spark  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--node", required=True, help="ssh alias / host (e.g. spark1)")
    ap.add_argument("--role", required=True, choices=["A", "B"])
    ap.add_argument("--run-setup", action="store_true", help="Seed the node via setup_node.sh before verifying")
    ap.add_argument("--only", default="", help="Comma-separated gate ids to run (default: all)")
    args = ap.parse_args()

    if args.run_setup:
        print(f"[setup] seeding {args.node} (role {args.role}) via ops/spark/setup_node.sh ...", flush=True)
        spark.run_setup(args.node, args.role)

    only = [g.strip() for g in args.only.split(",") if g.strip()] or None
    try:
        report = spark.verify(args.node, args.role, only=only)
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 2

    results = report["gates"]
    passed = [r for r in results if r["verdict"] == "PASS"]
    failed = [r for r in results if r["verdict"] != "PASS"]

    print("\n" + "=" * 60, flush=True)
    print(f"SPARK SECTION-A ACCEPTANCE :: {report['node']} (role {report['role']})", flush=True)
    for r in results:
        print(f"  [{r['verdict']}] {r['id']:14s} {r['title']}", flush=True)
        if r["verdict"] != "PASS":
            print(f"          machine: {r['machine_detail']}", flush=True)
            print(f"          gemini : {r['gemini_reason']}", flush=True)
            print(f"          fix    : {r['fix']}", flush=True)
    report_path = REPO_ROOT / "data" / "spark" / report["node"] / "acceptance.json"
    print(f"\n  {len(passed)}/{len(results)} gates green. Report: {report_path}", flush=True)
    print("=" * 60, flush=True)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
