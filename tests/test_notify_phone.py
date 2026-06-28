"""The delivery home's honesty contract — `src/notify_phone.py`.

These tests ARE part of the capability's definition of done:
  * "delivered" is written ONLY when Discord returns a message id.
  * a failed send is LOUD (failed ledger line + alarm) and NEVER reads as
    success — no silent #ucs fallback, no fabricated "delivered".
  * the ledger is write-ahead: pending precedes delivered/failed.

The single network seam `_post` is stubbed, so no real Discord call is made.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Base(unittest.TestCase):
    def setUp(self):
        import src.notify_phone as np

        self.np = np
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._orig = (np.STATE_DIR, np.LEDGER)
        np.STATE_DIR = str(tmp / "state")
        np.LEDGER = str(tmp / "notify.log")
        # never actually pop a macOS notification in CI
        self._notif = patch.object(np, "_local_notification", lambda *a, **k: None)
        self._notif.start()

    def tearDown(self):
        self.np.STATE_DIR, self.np.LEDGER = self._orig
        self._notif.stop()
        self._tmp.cleanup()

    def _ledger(self) -> list[dict]:
        try:
            with open(self.np.LEDGER, "r", encoding="utf-8") as fh:
                return [json.loads(x) for x in fh if x.strip()]
        except FileNotFoundError:
            return []

    def _statuses(self) -> list[str]:
        return [e.get("status") for e in self._ledger()]


class TestDeliverHonesty(_Base):
    ENV = {
        "DISCORD_APP_BOT_TOKEN": "tok",
        "AUTHORIZED_USER_IDS": "999",
        "DISCORD_TEXT_CHANNEL_ID": "555",
    }

    def test_delivered_only_with_message_id(self):
        def fake_post(token, path, body):
            if path.endswith("/channels"):
                return {"id": "DM1"}
            return {"id": "MSG1"}

        with patch.object(self.np, "_post", side_effect=fake_post):
            res = self.np.deliver("hi", project="p", sid="s", env=self.ENV)
        self.assertTrue(res.delivered)
        self.assertEqual(res.msg_id, "MSG1")
        # write-ahead: pending precedes delivered
        st = self._statuses()
        self.assertIn("pending", st)
        self.assertIn("delivered", st)
        self.assertLess(st.index("pending"), st.index("delivered"))

    def test_no_message_id_is_undelivered(self):
        def fake_post(token, path, body):
            if path.endswith("/channels"):
                return {"id": "DM1"}
            return {}  # Discord accepted nothing usable

        with patch.object(self.np, "_post", side_effect=fake_post):
            res = self.np.deliver("hi", project="p", sid="s", env=self.ENV)
        self.assertFalse(res.delivered)
        self.assertIn("failed", self._statuses())
        self.assertNotIn("delivered", self._statuses())

    def test_dms_disabled_is_loud_failure_not_a_lie(self):
        def fake_post(token, path, body):
            if path.endswith("/channels"):
                return {"id": "DM1"}
            raise self.np.NotifyError("HTTP 403", status=403, hint="enable DMs")

        with patch.object(self.np, "_post", side_effect=fake_post), \
             patch.object(self.np, "_alarm") as alarm:
            res = self.np.deliver("you missed X", project="lv5", sid="s", env=self.ENV)
        self.assertFalse(res.delivered)
        alarm.assert_called_once()              # the loud rung fired
        self.assertNotIn("delivered", self._statuses())
        self.assertIn("failed", self._statuses())

    def test_missing_secrets_alarms_and_fails(self):
        with patch.object(self.np, "_alarm") as alarm:
            res = self.np.deliver("x", env={"AUTHORIZED_USER_IDS": ""})
        self.assertFalse(res.delivered)
        alarm.assert_called_once()

    def test_never_blames_discord_for_5xx(self):
        # A 5xx must read as OUR notify path failing, surfaced — not "Discord down".
        def fake_post(token, path, body):
            if path.endswith("/channels"):
                return {"id": "DM1"}
            raise self.np.NotifyError("notify path failed: HTTP 503", status=503)

        with patch.object(self.np, "_post", side_effect=fake_post), \
             patch.object(self.np, "_alarm") as alarm:
            res = self.np.deliver("x", env=self.ENV)
        self.assertFalse(res.delivered)
        alarm.assert_called_once()


class TestEnvParsing(unittest.TestCase):
    """The hand `.env` parser must agree with python-dotenv on inline comments —
    a `DISCORD_TEXT_CHANNEL_ID=123 #ucs` that leaks the comment builds a broken
    URL (the real bug found by reproducing the live state)."""

    def test_clean_value(self):
        import src.notify_phone as np

        self.assertEqual(np._clean_value("123 #ucs"), "123")
        self.assertEqual(np._clean_value("123    #ucs"), "123")
        self.assertEqual(np._clean_value('"quoted value"'), "quoted value")
        self.assertEqual(np._clean_value("nocomment"), "nocomment")
        self.assertEqual(np._clean_value("has#hash"), "has#hash")  # no space => kept


class TestAlarmThrottle(_Base):
    """The loud alarm rungs are throttled per failure key so a persistently-down
    path nags once per window, not once per stop — without dropping ledger truth."""

    def test_first_fires_then_throttles_same_key(self):
        self.assertFalse(self.np._alarm_throttled("403"))   # first -> fire
        self.assertTrue(self.np._alarm_throttled("403"))    # repeat -> throttled
        self.assertFalse(self.np._alarm_throttled("500"))   # new reason -> fire


class TestStalePending(_Base):
    def test_unresolved_pending_is_counted(self):
        self.np.ledger_append({"status": "pending", "sid": "a"})
        self.np.ledger_append({"status": "pending", "sid": "b"})
        self.np.ledger_append({"status": "delivered", "sid": "a", "msg_id": "1"})
        self.assertEqual(self.np._stale_pending(), 1)  # only b dangles


if __name__ == "__main__":
    unittest.main(verbosity=2)
