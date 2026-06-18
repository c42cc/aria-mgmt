#!/usr/bin/env python3
"""DGX Spark — Claude Code workspace + headless run CLI (thin over src/spark.py).

One implementation, shared with Aria's spark_run* tools. Subcommands:

  sync    Stand up / update the live_visuals_4 CC workspace on a node
          (rsync repo + overlay control-plane + bootstrap). Idempotent.
  auth    Report whether the node's claude is on the Max subscription
          (--probe spends one tiny call to confirm a live round-trip).
  run     Launch the detached (tmux) audit+collapse run; prints the run id.
  status  Poll a run (tmux liveness, DONE/exit, branch/commit, last turn, cost).
  fetch   Pull a finished run's artifacts back (run.log, refreshed ledger, and
          an importable git bundle of the collapse branch).

USAGE
  .venv/bin/python scripts/spark_cc.py sync   --node spark1
  .venv/bin/python scripts/spark_cc.py auth   --node spark1 --probe
  .venv/bin/python scripts/spark_cc.py run    --node spark1
  .venv/bin/python scripts/spark_cc.py status --node spark1 --run-id cc_2026...
  .venv/bin/python scripts/spark_cc.py fetch  --node spark1 --run-id cc_2026...

The run uses the node's Max subscription; ANTHROPIC_API_KEY is stripped from the
run env so the metered key can never shadow it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import spark  # noqa: E402

DEFAULT_INSTRUCTION = REPO_ROOT / "ops" / "spark" / "audit_collapse_instruction.md"


def _emit(obj: dict) -> int:
    print(json.dumps(obj, indent=2))
    return 0 if obj.get("ok", True) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync", help="stand up / update the CC workspace on the node")
    p.add_argument("--node", required=True)
    p.add_argument("--mirror", action="store_true", help="rsync --delete (pristine re-mirror)")
    p.add_argument("--skip-bootstrap", action="store_true")
    p.add_argument("--smoke-gate", action="store_true", help="run quality_gate.sh after bootstrap")

    p = sub.add_parser("auth", help="report subscription auth status of the node's claude")
    p.add_argument("--node", required=True)
    p.add_argument("--probe", action="store_true", help="spend one tiny call to confirm a round-trip")

    p = sub.add_parser("run", help="launch the detached audit+collapse run")
    p.add_argument("--node", required=True)
    p.add_argument("--branch", default="")
    p.add_argument("--mode", default=spark.DEFAULT_RUN_MODE, choices=list(spark._VALID_MODES))
    p.add_argument("--model", default=spark.AUDIT_MODEL, help=f"default {spark.AUDIT_MODEL}")
    p.add_argument("--effort", default=spark.AUDIT_EFFORT, choices=list(spark._VALID_EFFORTS),
                   help=f"adaptive reasoning effort (default {spark.AUDIT_EFFORT})")
    p.add_argument("--extended-thinking", action="store_true",
                   help="enable extended thinking (default off for the audit)")
    p.add_argument("--instruction-file", default=str(DEFAULT_INSTRUCTION))
    p.add_argument("--force-unauthed", action="store_true")

    p = sub.add_parser("status", help="poll a run")
    p.add_argument("--node", required=True)
    p.add_argument("--run-id", required=True)

    p = sub.add_parser("fetch", help="pull a finished run's artifacts back")
    p.add_argument("--node", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--branch", default="")

    args = ap.parse_args()

    try:
        if args.cmd == "sync":
            return _emit(spark.sync_workspace(
                args.node, mirror=args.mirror,
                skip_bootstrap=args.skip_bootstrap, smoke_gate=args.smoke_gate))
        if args.cmd == "auth":
            return _emit(spark.cc_auth_status(args.node, probe=args.probe))
        if args.cmd == "run":
            instr = Path(args.instruction_file).read_text()
            return _emit(spark.run_audit(
                args.node, instr, branch=args.branch or None, mode=args.mode,
                model=args.model, effort=args.effort,
                extended_thinking=args.extended_thinking,
                force_unauthed=args.force_unauthed))
        if args.cmd == "status":
            return _emit(spark.run_status(args.node, args.run_id))
        if args.cmd == "fetch":
            return _emit(spark.fetch_results(args.node, args.run_id, branch=args.branch or None))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
