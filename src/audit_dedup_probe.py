"""Audit-log duplicate-call probe.

Reads `data/audit.jsonl` and flags any `(tool, args, session_key)` tuple that
fires more than once inside a short window. Each hit is a regression of the
API-duplication remediation. CLI exits non-zero when hits are found so this
target can fence CI / pre-restart checks.

Usage:
    python -m src.audit_dedup_probe                     # default: last 24h, 5s window
    python -m src.audit_dedup_probe --since 1           # last 1 hour
    python -m src.audit_dedup_probe --window 2.0        # tighter 2s window
    python -m src.audit_dedup_probe --max 0             # any number of hits fails
    python -m src.audit_dedup_probe --allow 5           # tolerate up to 5 hits

Allowlist rationale: preflight probes legitimately fire identical reads when
the operator runs `!preflight` manually right after boot. The `--allow N`
threshold accommodates that without blinding us to runaway-loop regressions.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

log = logging.getLogger(__name__)

DEFAULT_WINDOW_SEC = 5.0
DEFAULT_SINCE_HOURS = 24
DEFAULT_ALLOWED_HITS = 0

# Allowlist for probes that legitimately fire identical reads.
# This is for preflight smoke tests, not for product paths. Adding a tool
# here means "we explicitly accept that this can repeat within the window."
_PREFLIGHT_TOOL_ALLOWLIST = frozenset({
    "list_directory",        # probe_mcp_filesystem hits the same dir
    "calendar_calendars",    # probe_mcp_apple_calendar
    "execute_command",       # probe_mcp_shell echo PREFLIGHT_PING
    "execute.command",       # MCP-name-sanitized variant
})


@dataclass
class DupHit:
    tool: str
    session_key: str
    args_summary: str
    ts_first: str
    ts_second: str
    dt_seconds: float

    def is_preflight(self) -> bool:
        """True iff the tool is on the preflight allowlist AND session_key is empty.

        Product traffic always carries a session_key. Preflight probes do not.
        We allow the preflight allowlist only for empty-session-key rows so a
        real product duplicate of e.g. list_directory is still surfaced.
        """
        return self.tool in _PREFLIGHT_TOOL_ALLOWLIST and not self.session_key


def _iter_audit_rows(path: str, since_hours: int) -> Iterator[dict]:
    """Yield audit rows newer than `since_hours` ago.

    Raises (loudly) if the audit log doesn't exist or any row fails to parse —
    those are real signals, not noise to swallow.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"audit log not found at {path}. Run the bot at least once so "
            f"src/mcp.py can write the first entry."
        )

    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    with open(path) as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"audit log line {line_no} is not valid JSON: {exc}. "
                    f"Inspect {path}:{line_no}."
                )

            ts_str = row.get("ts")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts < cutoff:
                continue
            row["_ts"] = ts
            yield row


def find_dup_hits(
    audit_path: str,
    *,
    since_hours: int = DEFAULT_SINCE_HOURS,
    window_sec: float = DEFAULT_WINDOW_SEC,
) -> list[DupHit]:
    """Return every (tool, args, session_key) repeat fired inside `window_sec`."""
    by_key: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in _iter_audit_rows(audit_path, since_hours):
        key = (
            row.get("tool", ""),
            json.dumps(row.get("args", {}), sort_keys=True),
            row.get("session_key", ""),
        )
        by_key[key].append(row)

    hits: list[DupHit] = []
    for (tool, args_json, session_key), rows in by_key.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: r["_ts"])
        for prev, curr in zip(rows, rows[1:]):
            dt = (curr["_ts"] - prev["_ts"]).total_seconds()
            if dt <= window_sec:
                hits.append(DupHit(
                    tool=tool,
                    session_key=session_key,
                    args_summary=args_json[:200],
                    ts_first=prev["ts"],
                    ts_second=curr["ts"],
                    dt_seconds=dt,
                ))
    hits.sort(key=lambda h: h.ts_second)
    return hits


def _cli_main() -> int:
    parser = argparse.ArgumentParser(description="Detect duplicate API calls in audit.jsonl")
    parser.add_argument("--audit-log", default="data/audit.jsonl",
                        help="Path to audit.jsonl (default: data/audit.jsonl)")
    parser.add_argument("--since", type=int, default=DEFAULT_SINCE_HOURS,
                        help=f"Only scan rows from the last N hours (default {DEFAULT_SINCE_HOURS})")
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW_SEC,
                        help=f"Repeat-inside-this-many-seconds counts as a dup (default {DEFAULT_WINDOW_SEC})")
    parser.add_argument("--allow", type=int, default=DEFAULT_ALLOWED_HITS,
                        help="Maximum non-preflight hits before exiting non-zero")
    parser.add_argument("--include-preflight", action="store_true",
                        help="Don't filter out the preflight allowlist when counting hits")
    args = parser.parse_args()

    hits = find_dup_hits(
        args.audit_log,
        since_hours=args.since,
        window_sec=args.window,
    )

    product_hits = [h for h in hits if args.include_preflight or not h.is_preflight()]
    preflight_hits = [h for h in hits if h.is_preflight()]

    print(f"Audit dedup probe — last {args.since}h, window {args.window}s")
    print(f"Total dup pairs found: {len(hits)} "
          f"(product: {len(product_hits)}, preflight-allowlisted: {len(preflight_hits)})")
    print()

    if product_hits:
        print("PRODUCT-PATH DUPLICATES (these are regressions):")
        for h in product_hits:
            sk = h.session_key[:8] if h.session_key else "(none)"
            print(f"  {h.tool:35s}  sess={sk:8s}  dt={h.dt_seconds:5.2f}s  "
                  f"at {h.ts_second}  args={h.args_summary}")
    else:
        print("No product-path duplicates.")

    if preflight_hits and args.include_preflight:
        print()
        print("PREFLIGHT-ALLOWLIST DUPLICATES (informational):")
        for h in preflight_hits:
            print(f"  {h.tool:35s}  sess=(none)   dt={h.dt_seconds:5.2f}s  "
                  f"at {h.ts_second}  args={h.args_summary}")

    if len(product_hits) > args.allow:
        print()
        print(f"FAIL: {len(product_hits)} product-path duplicates exceed allow={args.allow}")
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    sys.exit(_cli_main())
