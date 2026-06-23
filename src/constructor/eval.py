"""UCS Evaluation Layer — offline prompt quality scoring.

Run via CLI:  python -m src.constructor.eval [command]

Governance: this module ADVISES. It never calls save_template or
rollback_template. User voice edits always win. See ARCHITECTURE.md
Fundamental 13.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..db import get_connection

log = logging.getLogger(__name__)


@dataclass
class VersionScore:
    prompt_name: str
    version: int
    origin: str
    metric: str
    score: float
    sample_size: int


class EvalRunner:
    """Scores prompt versions using real execution data."""

    def approval_rate(self, prompt_name: str, version: int | None = None) -> list[VersionScore]:
        """Compute approval rate per prompt version.

        Heuristic: after a plan_with_claude call using prompt_template=prompt_name,
        if the next tool call in the same session_key is build_with_cursor, the
        plan was approved. If it's another plan_with_claude, it was a revision.

        Returns one VersionScore per version (or one for the specified version).
        """
        with get_connection() as conn:
            query = """
                SELECT le.session_key, le.prompt_template, le.started_at,
                       pv.version, pv.origin
                FROM loop_executions le
                LEFT JOIN prompt_versions pv
                    ON pv.prompt_name = le.prompt_template
                    AND pv.created_at <= le.started_at
                WHERE le.tool_name = 'plan_with_claude'
                  AND le.prompt_template = ?
                ORDER BY le.session_key, le.started_at
            """
            plan_rows = conn.execute(query, (prompt_name,)).fetchall()

            all_exec_rows = conn.execute(
                "SELECT tool_name, session_key, started_at "
                "FROM loop_executions ORDER BY session_key, started_at"
            ).fetchall()

        exec_by_session: dict[str, list[dict]] = {}
        for r in all_exec_rows:
            sk = r["session_key"] or ""
            exec_by_session.setdefault(sk, []).append(dict(r))

        version_stats: dict[int, dict[str, int]] = {}

        for row in plan_rows:
            sk = row["session_key"] or ""
            plan_time = row["started_at"]
            v = row["version"] if row["version"] is not None else 0
            origin = row["origin"] or "unknown"

            if version is not None and v != version:
                continue

            if v not in version_stats:
                version_stats[v] = {"approved": 0, "revised": 0, "unknown": 0, "origin": origin}

            session_execs = exec_by_session.get(sk, [])
            found_next = False
            for ex in session_execs:
                if ex["started_at"] <= plan_time:
                    continue
                if ex["tool_name"] == "build_with_cursor":
                    version_stats[v]["approved"] += 1
                elif ex["tool_name"] == "plan_with_claude":
                    version_stats[v]["revised"] += 1
                else:
                    version_stats[v]["unknown"] += 1
                found_next = True
                break

            if not found_next:
                version_stats[v]["unknown"] += 1

        results = []
        for v, stats in sorted(version_stats.items()):
            total = stats["approved"] + stats["revised"]
            rate = stats["approved"] / total if total > 0 else 0.0
            results.append(VersionScore(
                prompt_name=prompt_name,
                version=v,
                origin=stats.get("origin", "unknown"),
                metric="approval_rate",
                score=rate,
                sample_size=total,
            ))

        return results

    def compare_versions(self, prompt_name: str) -> list[VersionScore]:
        """Compare approval rates across all versions of a prompt."""
        return self.approval_rate(prompt_name)

    def suggest_rollback(self, prompt_name: str) -> dict[str, Any] | None:
        """If the current version's approval rate is worse than the best prior, suggest rollback."""
        scores = self.compare_versions(prompt_name)
        if len(scores) < 2:
            return None

        current = scores[-1]
        best_prior = max(scores[:-1], key=lambda s: s.score)

        if current.score < best_prior.score and best_prior.sample_size >= 2:
            return {
                "recommendation": "rollback",
                "current_version": current.version,
                "current_score": current.score,
                "better_version": best_prior.version,
                "better_score": best_prior.score,
                "note": "This is a suggestion. User voice edits always win.",
            }

        return None

    def save_results(self, scores: list[VersionScore]) -> int:
        """Persist eval results to eval_results table."""
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        with get_connection() as conn:
            for s in scores:
                conn.execute(
                    "INSERT INTO eval_results "
                    "(prompt_name, prompt_version, metric, score, sample_size, detail_json, evaluated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (s.prompt_name, s.version, s.metric, s.score, s.sample_size, None, now),
                )
                count += 1
        return count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from ..db import init_db
    init_db()

    args = sys.argv[1:]
    runner = EvalRunner()

    if not args or args[0] == "help":
        print("Usage: python -m src.constructor.eval <command> [prompt_name]")
        print()
        print("Commands:")
        print("  rate <prompt_name>     Show approval rates per version")
        print("  compare <prompt_name>  Compare all versions")
        print("  suggest <prompt_name>  Suggest rollback if current version is worse")
        print("  all                    Show rates for all prompt templates")
        return 0

    cmd = args[0]

    if cmd == "all":
        from .prompts import list_templates
        for name in list_templates():
            scores = runner.compare_versions(name)
            if not scores:
                print(f"{name}: no execution data")
                continue
            for s in scores:
                print(f"{name} v{s.version} ({s.origin}): {s.score:.0%} approval ({s.sample_size} samples)")
        return 0

    if len(args) < 2:
        print(f"Error: '{cmd}' requires a prompt_name argument")
        return 1

    prompt_name = args[1]

    if cmd == "rate":
        scores = runner.approval_rate(prompt_name)
        if not scores:
            print(f"No execution data for prompt '{prompt_name}'")
            return 0
        for s in scores:
            print(f"v{s.version} ({s.origin}): {s.score:.0%} approval ({s.sample_size} samples)")
        runner.save_results(scores)
        return 0

    if cmd == "compare":
        scores = runner.compare_versions(prompt_name)
        if not scores:
            print(f"No execution data for prompt '{prompt_name}'")
            return 0
        for s in scores:
            print(f"v{s.version} ({s.origin}): {s.score:.0%} approval ({s.sample_size} samples)")
        return 0

    if cmd == "suggest":
        suggestion = runner.suggest_rollback(prompt_name)
        if suggestion is None:
            print(f"No rollback suggested for '{prompt_name}'")
            return 0
        print(json.dumps(suggestion, indent=2))
        return 0

    print(f"Unknown command: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(_cli_main())
