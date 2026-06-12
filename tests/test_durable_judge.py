"""Proof that judging is durable and idempotent, and that thread recency is
computed in code.

Two deterministic-edge fixes:

1. The inline judge was a fire-and-forget `asyncio.create_task` dropped on
   process churn, so ~45% of sessions (worst-when-worst) went unjudged. The
   replacement is a DB worklist (`get_unjudged_records`) drained by a periodic
   sweep (`sweep_unjudged`) — idempotent via a LEFT JOIN, so a judged record
   never reappears and a second sweep is a no-op.

2. `humanize_age` computes relative recency ("8h ago") in code so the model
   never does (and gets wrong) elapsed-time math.

Run with:
    .venv/bin/python -m unittest tests.test_durable_judge -v
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _IsolatedDB:
    """Point src.db at a throwaway sqlite file for the duration of a test."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "state.db")
        self._patcher = patch("src.db.DB_PATH", self.path)

    def __enter__(self):
        self._patcher.start()
        from src.db import init_db
        init_db()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        self._tmp.cleanup()
        return False


class TestUnjudgedWorklist(unittest.TestCase):
    def test_record_appears_then_disappears_after_verdict(self):
        with _IsolatedDB():
            from src import db
            rid = db.record_session(
                session_key="K", tool_name="do_with_claude",
                inputs={"task": "x"}, outputs={"result": "ok"}, context=None,
            )
            self.assertIsNotNone(rid)

            ids = [r["id"] for r in db.get_unjudged_records(hours=24)]
            self.assertIn(rid, ids)

            # Once a verdict references the record id, the LEFT JOIN drops it.
            db.write_verdict("agent", str(rid), "correct", 1.0, [])
            ids_after = [r["id"] for r in db.get_unjudged_records(hours=24)]
            self.assertNotIn(rid, ids_after)

    def test_non_judge_worthy_product_is_never_in_worklist(self):
        with _IsolatedDB():
            from src import db
            # "system"-mapped tools are not recorded at all; use a tool that
            # maps to a non-judge-worthy product via record_session directly.
            rid = db.record_session(
                session_key="K", tool_name="some_unknown_tool",
                inputs={}, outputs={"result": "ok"}, context=None,
            )
            self.assertIsNotNone(rid)  # recorded as product "unknown"
            ids = [r["id"] for r in db.get_unjudged_records(hours=24)]
            self.assertNotIn(rid, ids)


class TestSweepIdempotent(unittest.IsolatedAsyncioTestCase):
    async def test_sweep_judges_once_then_is_a_noop(self):
        with _IsolatedDB():
            from src import db, judge
            rid = db.record_session(
                session_key="K", tool_name="do_with_claude",
                inputs={}, outputs={"result": "hi"}, context=None,
            )

            calls: list[int] = []

            async def fake_eval(record_id: int, product: str):
                calls.append(record_id)
                v = judge.Verdict(
                    product=product, session_id=str(record_id),
                    verdict="failed", score=0.1, reasons=["nope"],
                    judged_at="now",
                )
                # The real evaluate_record writes the verdict; mirror that so
                # the LEFT JOIN sees it on the next sweep.
                db.write_verdict(product, str(record_id), v.verdict, v.score, v.reasons)
                return v

            alerts: list[str] = []

            async def fake_alert(msg: str):
                alerts.append(msg)

            with patch("src.judge.evaluate_record", new=fake_eval):
                n1 = await judge.sweep_unjudged(hours=24, alert=fake_alert)
                n2 = await judge.sweep_unjudged(hours=24, alert=fake_alert)

            self.assertEqual(n1, 1)
            self.assertEqual(n2, 0)            # idempotent — nothing left to judge
            self.assertEqual(calls, [rid])     # evaluated exactly once
            self.assertEqual(len(alerts), 1)   # failed verdict alerted once
            self.assertIn("FAILED", alerts[0])


class TestHumanizeAge(unittest.TestCase):
    def test_boundaries(self):
        from src.cursor_tools import humanize_age
        now = time.time()
        self.assertEqual(humanize_age(now - 5), "just now")
        self.assertEqual(humanize_age(now - 120), "2m ago")
        self.assertEqual(humanize_age(now - 8 * 3600), "8h ago")
        self.assertEqual(humanize_age(now - 3 * 86400), "3d ago")

    def test_future_clamps_to_just_now(self):
        from src.cursor_tools import humanize_age
        self.assertEqual(humanize_age(time.time() + 500), "just now")

    def test_bad_input_is_unknown(self):
        from src.cursor_tools import humanize_age
        self.assertEqual(humanize_age(None), "unknown")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main(verbosity=2)
