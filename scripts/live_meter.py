#!/usr/bin/env python3
"""scripts/live_meter.py — the ONE real meter: does a real request genuinely
complete on the build that is running?

This is the acceptance test the dysfunction post-mortem said was missing: every
other "green" (preflight, a unit test, a rebuilt branch, an offline prompt score)
is a proxy. This one submits a real `do_with_claude` request against the real MCP
fleet and real Claude, and asserts a genuine success — not a Blocked wall, not an
error, with a real tool actually fired.

Why a script and not the live pid over HTTP: the proof is keyed to the BUILD HASH
(src/build_hash.py). The CRITICAL `deployed_trunk` preflight gate guarantees the
running process IS the committed trunk build, so a success keyed to that build
hash is a success for the live pid — the build hash is the bridge, and it makes
the meter deterministic + gateable instead of dependent on bot uptime. The meter
therefore REFUSES to certify a non-trunk or dirty tree: you cannot prove a build
you have not committed.

The receipt lands at data/receipts/<build_hash>.json. A receipt is valid only for
the exact build that produced it; any source change to the build changes the hash
and the old receipt no longer answers "is THIS build proven?".

Run:  python scripts/live_meter.py            # certify the current trunk build
      python scripts/live_meter.py --dev       # run off-trunk, receipt marked non-certifying
      python scripts/live_meter.py --task "…"  # custom request
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

# Make `src` importable whether or not the editable install is present.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# The default meter task: a real MCP round-trip (filesystem is a CRITICAL preflight
# capability, so it is the most reliable real action) plus a real answer from
# Claude. Genuine success means a tool fired AND the loop answered without walling.
DEFAULT_TASK = (
    "Use the filesystem tool to list your allowed directories, then tell me in one "
    "sentence how many allowed directories there are."
)

_BLOCK_MARKER = "**Blocked"  # format_block() prefix in src/outcomes.py


def classify_meter_result(result: str | None, tool_fired: bool) -> tuple[bool, str]:
    """Pure: did the request genuinely succeed? (ok, reason).

    Genuine success requires a non-empty answer, that the loop did NOT hit a wall
    (no Blocked), that it is not a raw error envelope, AND that a real tool fired
    (the request caused real action, not just talk). This is the v0 definition;
    Step 6 wires the calibrated judge to gate a Task's done claim more richly.
    """
    if not result or not result.strip():
        return False, "empty result (the loop produced nothing)"
    stripped = result.lstrip()
    if stripped.startswith(_BLOCK_MARKER):
        first_line = result.strip().splitlines()[0]
        return False, f"blocked, not done: {first_line}"
    if stripped.startswith('{"error"') or stripped.startswith("{'error'"):
        return False, f"error envelope returned: {stripped[:160]}"
    if not tool_fired:
        return False, "no tool fired — the loop answered from talk alone, not real action"
    return True, "genuine success: a real tool fired and the loop answered without walling"


def build_receipt(
    *,
    build_hash: str,
    head_sha: str,
    dirty: bool,
    branch: str,
    certifies_trunk: bool,
    task: str,
    result: str | None,
    tool_fired: bool,
    ok: bool,
    verdict_reason: str,
) -> dict:
    """Pure: the build-hash-keyed proof receipt. `certifies_trunk` is False for a
    --dev run so a non-trunk smoke can never be mistaken for a real proof."""
    return {
        "meter": "live_outcome",
        "build_hash": build_hash,
        "head_sha": head_sha,
        "dirty": dirty,
        "branch": branch,
        "certifies_trunk": certifies_trunk,
        "task": task,
        "tool_fired": tool_fired,
        "ok": ok,
        "verdict": "correct" if ok else "failed",
        "verdict_reason": verdict_reason,
        "result_preview": (result or "")[:1500],
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def _receipts_dir() -> str:
    return os.path.join(_REPO_ROOT, "data", "receipts")


def write_receipt(receipt: dict) -> str:
    """Write the receipt keyed to the build hash; atomic (temp -> rename)."""
    os.makedirs(_receipts_dir(), exist_ok=True)
    path = os.path.join(_receipts_dir(), f"{receipt['build_hash']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(receipt, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


async def run_meter(task: str, *, timeout_sec: float = 240.0) -> tuple[bool, str, bool]:
    """Run one real request through the live build. Returns (ok, result, tool_fired).
    Loud: an exception in the fleet or the loop propagates."""
    from src.mcp import init_mcp
    from src.memory import init_memory
    from src import tools

    await init_mcp()
    # Initialize the same tool deps the bot does (the Anthropic client + mem0).
    # The meter has no cursor bridge or callbacks — its task is a read-only MCP
    # round-trip, so None is fine; those are only used by other tool paths.
    tools.init_tools(None)
    init_memory()
    # A UNIQUE session per run: a reused key carries the prior run's findings
    # ledger, so the loop answers "from the previous run's findings" without
    # firing a tool — and the meter must exercise a REAL action every time.
    session_key = f"live_meter:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    result = await asyncio.wait_for(
        tools._do_with_claude(task, session_key=session_key), timeout=timeout_sec
    )
    state = tools._state_for(session_key)
    tool_fired = bool(getattr(state, "last_tool_trace", None))
    ok, reason = classify_meter_result(result, tool_fired)
    return ok, result, tool_fired


async def _amain(task: str, allow_nontrunk: bool) -> int:
    from src import build_hash as bh

    branch = bh.current_branch()
    dirty_raw = bh.build_tree_dirty()
    dirty = bool(dirty_raw)
    build_hash = bh.compute_build_hash()
    head_sha = bh._git(["rev-parse", "HEAD"]) or "nogit"
    certifies_trunk = (branch == bh.TRUNK) and not dirty

    if not certifies_trunk and not allow_nontrunk:
        print(
            f"live_meter: REFUSE to certify — not a clean trunk build "
            f"(branch={branch!r} dirty={dirty}). A live-outcome receipt must certify "
            f"the committed trunk. Land on '{bh.TRUNK}' and commit, then re-run "
            f"(or pass --dev to smoke the plumbing without certifying)."
        )
        return 2

    print(f"live_meter: build_hash={build_hash[:12]} branch={branch} dirty={dirty} "
          f"certifies_trunk={certifies_trunk}")
    print(f"live_meter: task = {task!r}")

    ok, result, tool_fired = await run_meter(task)
    _, reason = classify_meter_result(result, tool_fired)
    receipt = build_receipt(
        build_hash=build_hash,
        head_sha=head_sha,
        dirty=dirty,
        branch=branch,
        certifies_trunk=certifies_trunk,
        task=task,
        result=result,
        tool_fired=tool_fired,
        ok=ok,
        verdict_reason=reason,
    )
    path = write_receipt(receipt)

    print(f"\nlive_meter: tool_fired={tool_fired} ok={ok}")
    print(f"live_meter: verdict={receipt['verdict']} — {reason}")
    print(f"live_meter: receipt -> {path}")
    print(f"\n--- result ---\n{(result or '').strip()[:800]}\n--------------")
    # The MCP fleet's stdio teardown raises a benign cross-task cancel-scope
    # error on loop close AFTER the verdict + receipt are done; hard-exit so that
    # teardown noise can't mask the real exit code. The receipt is the truth.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if ok else 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="The one live-outcome meter.")
    parser.add_argument("--task", default=DEFAULT_TASK, help="the real request to run")
    parser.add_argument(
        "--dev", action="store_true",
        help="run off-trunk/dirty; the receipt is marked certifies_trunk=false",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args.task, allow_nontrunk=args.dev))


if __name__ == "__main__":
    sys.exit(main())
