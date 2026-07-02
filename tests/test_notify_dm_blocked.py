"""Contract: a STANDING owner-side DM block reroutes delivery, honestly.

Forensic 2026-07-02 01:49: the DM leg had NEVER delivered once since birth —
every stop failed 403/50278 ("no mutual guilds", returned even with a verified
mutual guild when the owner's privacy settings refuse bot DMs) — and the
30-minute alarm throttle had turned that one dead leg into an all-night
`NOTIFY PATH DOWN` @mention nag. The alarms were the only thing reaching the
phone; every real notification was dropped.

The contract now:

  * Discord 50007/50278 is a STANDING block, not an outage: the SAME content is
    delivered as an @mention in the text channel — a real send, a real message
    id — and the ledger names the leg (`via: channel_mention`). It NEVER
    masquerades as a DM.
  * The block itself is surfaced once a DAY (with the owner's one-tap fix),
    not per stop; the 9am heartbeat keeps proving the path.
  * A DM that works is still `via: dm`. Any non-standing failure stays LOUD
    (alarm + `failed` ledger line) and never fabricates success.

Run with:
    .venv/bin/python -m pytest tests/test_notify_dm_blocked.py -q
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


ENV = {
    "DISCORD_APP_BOT_TOKEN": "tok",
    "AUTHORIZED_USER_IDS": "42",
    "DISCORD_TEXT_CHANNEL_ID": "999",
}


def _blocked(code: int):
    from src import notify_phone

    return notify_phone.NotifyError(
        f"notify path failed: HTTP 403 code {code}", status=403,
        hint=notify_phone.DM_BLOCK_FIX, code=code,
    )


class NotifyDmBlocked(unittest.TestCase):
    def setUp(self):
        from src import notify_phone

        self.np = notify_phone
        self.tmp = tempfile.mkdtemp()
        self._patches = [
            patch.object(notify_phone, "STATE_DIR", os.path.join(self.tmp, "state")),
            patch.object(notify_phone, "LEDGER", os.path.join(self.tmp, "notify.log")),
            patch.object(notify_phone, "_dm_channel_id", lambda token, uid: "dmchan"),
            patch.object(notify_phone, "_local_notification", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def _ledger(self) -> list[dict]:
        try:
            with open(os.path.join(self.tmp, "notify.log")) as fh:
                return [json.loads(l) for l in fh if l.strip()]
        except FileNotFoundError:
            return []

    def _send_router(self, sends: list, *, dm_code: int | None = 50278):
        """A _send that refuses the DM channel with a standing block and
        accepts channel sends, recording every accepted post."""
        def _send(token, channel_id, content):
            if channel_id == "dmchan":
                raise _blocked(dm_code)
            sends.append((channel_id, content))
            return f"m{len(sends)}"
        return _send

    def test_standing_block_delivers_via_channel_mention(self):
        sends: list = []
        with patch.object(self.np, "_send", self._send_router(sends)):
            res = self.np.deliver("✅ live_visuals_5 — finished\nLast: gate green",
                                  kind="finished", project="lv5", sid="s1", env=ENV)
        self.assertTrue(res.delivered)
        self.assertEqual(res.via, "channel_mention")
        self.assertTrue(res.msg_id)
        # The content landed in the text channel, mention ensured (it pushes).
        chan, content = sends[0]
        self.assertEqual(chan, "999")
        self.assertIn("<@42>", content)
        self.assertIn("finished", content)
        # The ledger names the leg and the block — never a bare DM "delivered".
        delivered = [e for e in self._ledger() if e.get("status") == "delivered"]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["via"], "channel_mention")
        self.assertEqual(delivered[0]["dm_blocked"], 50278)

    def test_block_notice_fires_once_a_day_not_per_stop(self):
        sends: list = []
        with patch.object(self.np, "_send", self._send_router(sends)):
            self.np.deliver("stop one", kind="finished", project="p", sid="a", env=ENV)
            self.np.deliver("stop two", kind="finished", project="p", sid="b", env=ENV)
            self.np.deliver("stop three", kind="finished", project="p", sid="c", env=ENV)
        notices = [c for _, c in sends if "DM leg is blocked" in c]
        self.assertEqual(len(notices), 1, "the standing-block notice must not nag per stop")
        self.assertIn("one direct message", notices[0])  # the owner's real one-tap fix
        # Three real deliveries still landed (content is never throttled).
        delivered = [e for e in self._ledger() if e.get("status") == "delivered"]
        self.assertEqual(len(delivered), 3)

    def test_alarm_window_is_daily_not_thirty_minutes(self):
        self.assertGreaterEqual(self.np.ALARM_WINDOW_SEC, 86400.0)

    def test_working_dm_stays_via_dm(self):
        with patch.object(self.np, "_send", lambda t, c, x: "m9"):
            res = self.np.deliver("hi", kind="finished", project="p", sid="s", env=ENV)
        self.assertTrue(res.delivered)
        self.assertEqual(res.via, "dm")
        delivered = [e for e in self._ledger() if e.get("status") == "delivered"]
        self.assertEqual(delivered[0]["via"], "dm")

    def test_non_standing_failure_stays_loud_and_failed(self):
        def _send(token, channel_id, content):
            raise self.np.NotifyError("HTTP 500 boom", status=500)

        alarms: list = []
        with patch.object(self.np, "_send", _send), \
             patch.object(self.np, "_alarm", lambda *a, **k: alarms.append(k)):
            res = self.np.deliver("hi", kind="finished", project="p", sid="s", env=ENV)
        self.assertFalse(res.delivered)
        self.assertTrue(alarms, "a real outage must still alarm loudly")
        failed = [e for e in self._ledger() if e.get("status") == "failed"]
        self.assertEqual(len(failed), 1)

    def test_both_legs_down_is_a_loud_failure_never_a_lie(self):
        def _send(token, channel_id, content):
            raise _blocked(50278) if channel_id == "dmchan" else self.np.NotifyError(
                "HTTP 500 channel down", status=500
            )

        alarms: list = []
        with patch.object(self.np, "_send", _send), \
             patch.object(self.np, "_alarm", lambda *a, **k: alarms.append(k)):
            res = self.np.deliver("hi", kind="finished", project="p", sid="s", env=ENV)
        self.assertFalse(res.delivered)
        self.assertTrue(alarms)
        self.assertFalse([e for e in self._ledger() if e.get("status") == "delivered"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
