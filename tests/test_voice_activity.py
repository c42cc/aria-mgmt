"""The source-of-truth voice log: 'when did Aria last fire?' is a tail, not a
grep through stderr."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestVoiceActivity(unittest.TestCase):
    def setUp(self):
        import src.voice_activity as va

        self.va = va
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = va.LEDGER
        va.LEDGER = str(Path(self._tmp.name) / "voice_activity.jsonl")

    def tearDown(self):
        self.va.LEDGER = self._orig
        self._tmp.cleanup()

    def test_records_and_finds_last_fired(self):
        self.va.log("wake", source="local")
        self.va.log("heard", text="aria what time is it")
        self.va.log("spoke", text="It is nine o'clock.")
        self.va.log("heard", text="go away for two hours")
        self.va.log("go_away", seconds=7200, how="voice")

        self.assertEqual(len(self.va.tail(10)), 5)
        last = self.va.last_fired()
        self.assertIsNotNone(last)
        self.assertEqual(last["event"], "spoke")
        self.assertIn("nine o'clock", last["text"])

    def test_no_fired_yet(self):
        self.va.log("wake", source="local")
        self.assertIsNone(self.va.last_fired())


if __name__ == "__main__":
    unittest.main(verbosity=2)
