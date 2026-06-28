"""The deterministic trigger — `hooks/notify-finish.py`.

The capability's definition of done, encoded:
  * THE 7:30 REGRESSION: a transcript that reaches `turn_ended` and then writes
    MORE turns still fires (the byte-quiet timer that re-armed and dropped this
    is gone). The trigger is the `stop` hook + a grew-since-last-buzz offset.
  * EXACTLY-ONCE: one hand-back == one dispatch; a duplicate hook delivery for
    the same offset does NOT re-buzz.
  * FAIL-OPEN: a user-cancel is skipped, but an unknown status still sends.
  * ENRICHMENT is best-effort: intent (<user_query>) + last words + status.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent


def _load_hook():
    spec = importlib.util.spec_from_file_location(
        "notify_finish_mod", str(ROOT / "hooks" / "notify-finish.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _line(obj) -> str:
    return json.dumps(obj) + "\n"


# A transcript shaped exactly like the 7:30 failure: a turn finishes, THEN the
# user sends another message and the agent starts replying (turn_ended is NOT
# the final bytes).
SEVEN_THIRTY = (
    _line({"role": "user", "message": {"content": "<user_query>make the logo spin</user_query>"}})
    + _line({"role": "assistant", "message": {"content": "Done — the logo spins now."}})
    + _line({"type": "turn_ended", "status": "success"})
    + _line({"role": "user", "message": {"content": "<user_query>now make it pulse</user_query>"}})
    + _line({"role": "assistant", "message": {"content": "Working on the pulse..."}})
)


class TestHelpers(unittest.TestCase):
    def setUp(self):
        self.nf = _load_hook()

    def test_sid_from_transcript_path(self):
        p = "/Users/x/.cursor/projects/proj/agent-transcripts/ABC123/ABC123.jsonl"
        self.assertEqual(self.nf._sid(p), "ABC123")

    def test_clean_intent_pulls_user_query(self):
        txt = "<tools>...</tools><user_query>do the thing</user_query>"
        self.assertEqual(self.nf._clean_intent(txt), "do the thing")

    def test_enrich_reads_status_intent_and_last_words(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(SEVEN_THIRTY)
            path = fh.name
        status, intent, last = self.nf._enrich(path)
        self.assertEqual(status, "success")
        self.assertIn("pulse", intent)          # most-recent user ask
        self.assertIn("pulse", last.lower())     # most-recent assistant words

    def test_format_is_glanceable(self):
        msg = self.nf._format("live_visuals_5", "success", "make it spin", "all done")
        self.assertIn("live_visuals_5", msg)
        self.assertIn("finished", msg)
        self.assertIn("You asked", msg)


class TestDedupAndRegression(unittest.TestCase):
    def setUp(self):
        self.nf = _load_hook()
        self._tmp = tempfile.TemporaryDirectory()
        self.nf.STATE_DIR = str(Path(self._tmp.name) / "state")
        self.nf.OUTBOX_DIR = str(Path(self._tmp.name) / "outbox")
        self.transcript = str(Path(self._tmp.name) / "t.jsonl")

    def tearDown(self):
        self._tmp.cleanup()

    def _run_stop(self, dispatch):
        payload = {
            "transcript_path": self.transcript,
            "workspace_root": "/Users/x/PycharmProjects/agi_env_v1/live_visuals_5",
        }
        with patch.object(self.nf, "_dispatch", dispatch), \
             patch.object(sys, "argv", ["notify-finish.py", "stop"]), \
             patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            self.nf.main()

    def test_seven_thirty_shape_fires_and_is_exactly_once(self):
        # Write the 7:30 transcript (turn_ended NOT at the tail).
        with open(self.transcript, "w") as fh:
            fh.write(SEVEN_THIRTY)

        d1 = MagicMock()
        self._run_stop(d1)
        d1.assert_called_once()                 # the buzz the old timer dropped
        sent = d1.call_args.args[0]
        self.assertIn("live_visuals_5", sent)

        # A duplicate stop for the SAME bytes must not re-buzz (exactly-once).
        d2 = MagicMock()
        self._run_stop(d2)
        d2.assert_not_called()

        # A genuinely new hand-back (more bytes) buzzes again.
        with open(self.transcript, "a") as fh:
            fh.write(_line({"type": "turn_ended", "status": "success"}))
        d3 = MagicMock()
        self._run_stop(d3)
        d3.assert_called_once()

    def test_user_cancel_is_skipped_fail_open_on_unknown(self):
        # Explicit cancel -> skip.
        with open(self.transcript, "w") as fh:
            fh.write(_line({"role": "assistant", "message": {"content": "stopping"}}))
            fh.write(_line({"type": "turn_ended", "status": "cancelled"}))
        d = MagicMock()
        self._run_stop(d)
        d.assert_not_called()

    def test_unknown_status_still_sends(self):
        # No turn_ended at all -> status unknown -> fail-OPEN, we still buzz.
        with open(self.transcript, "w") as fh:
            fh.write(_line({"role": "assistant", "message": {"content": "handed back"}}))
        d = MagicMock()
        self._run_stop(d)
        d.assert_called_once()

    def test_non_stop_hook_is_ignored(self):
        with open(self.transcript, "w") as fh:
            fh.write(SEVEN_THIRTY)
        d = MagicMock()
        payload = {"transcript_path": self.transcript, "workspace_root": "/x/live_visuals_5"}
        with patch.object(self.nf, "_dispatch", d), \
             patch.object(sys, "argv", ["notify-finish.py", "subagentStop"]), \
             patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            self.nf.main()
        d.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
