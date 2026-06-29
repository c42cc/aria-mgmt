"""The 'go away for N' presence primitive + the voice gates.

  * one durable `away_until`: set/clear/is_away/remaining/describe.
  * `parse_duration` accepts the ways Corbin would say/type it.
  * the wake-word handler and voice auto-join FALL SILENT while away (the root
    fix for the 3 AM false-wake self-talk) and resume when it passes.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestPresenceState(unittest.TestCase):
    def setUp(self):
        import src.presence as p

        self.p = p
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = p.STATE
        p.STATE = str(Path(self._tmp.name) / "presence.json")

    def tearDown(self):
        self.p.STATE = self._orig
        self._tmp.cleanup()

    def test_default_present(self):
        self.assertFalse(self.p.is_away())
        self.assertEqual(self.p.describe(), "present")

    def test_set_and_clear(self):
        self.p.set_away(1800)
        self.assertTrue(self.p.is_away())
        self.assertGreater(self.p.remaining(), 1700)
        self.p.clear_away()
        self.assertFalse(self.p.is_away())

    def test_auto_resume_when_past(self):
        self.p.set_away(1800)
        future = time.time() + 1801
        self.assertFalse(self.p.is_away(now=future))  # the timer expires on its own

    def test_describe(self):
        self.p.set_away(30 * 60)
        self.assertIn("30m", self.p.describe())
        self.p.set_away(9 * 3600)
        self.assertIn("9h", self.p.describe())


class TestParseDuration(unittest.TestCase):
    def test_variants(self):
        from src import presence as p

        self.assertEqual(p.parse_duration("30m"), 1800)
        self.assertEqual(p.parse_duration("30 minutes"), 1800)
        self.assertEqual(p.parse_duration("9h"), 32400)
        self.assertEqual(p.parse_duration("9 hours"), 32400)
        self.assertEqual(p.parse_duration("90s"), 90)
        self.assertEqual(p.parse_duration("45"), 2700)  # bare number => minutes
        self.assertIsNone(p.parse_duration("soon"))
        self.assertIsNone(p.parse_duration(""))


class TestMatchGoAway(unittest.TestCase):
    """The DETERMINISTIC trigger: 'go away' is recognized from the transcript,
    not a model tool-call (which never fired — she chatted back instead)."""

    def test_exact_phrase_with_duration(self):
        from src import presence as p

        self.assertEqual(p.match_go_away("area go away for 2 hours"), 7200.0)
        self.assertEqual(p.match_go_away("go away for 30 minutes"), 1800.0)
        self.assertEqual(p.match_go_away("go to sleep for an hour"), 3600.0)

    def test_bare_go_away_defaults(self):
        from src import presence as p

        self.assertEqual(p.match_go_away("go away"), p.DEFAULT_AWAY_SECONDS)
        self.assertEqual(p.match_go_away("leave me alone"), p.DEFAULT_AWAY_SECONDS)
        self.assertEqual(p.match_go_away("be quiet"), p.DEFAULT_AWAY_SECONDS)

    def test_no_false_trigger(self):
        from src import presence as p

        self.assertIsNone(p.match_go_away("how can I help you today"))
        self.assertIsNone(p.match_go_away("don't go away"))
        self.assertIsNone(p.match_go_away("okay I'm here when you want"))
        self.assertIsNone(p.match_go_away(""))


class TestStopPhraseSilencesImmediately(unittest.IsolatedAsyncioTestCase):
    """When 'go away' is heard, the session silences her output THIS INSTANT
    (drops queued audio) and fires the stop callback — no audio, no tool-call."""

    async def test_go_away_silences_and_fires_silently(self):
        import asyncio
        from src.gemini_session import GeminiSession

        fired: list[float] = []

        async def cb(secs: float) -> None:
            fired.append(secs)

        s = GeminiSession(stop_phrase_callback=cb)
        s._audio_out_queue.put_nowait(b"her-pending-audio")  # she'd started talking
        s._user_turn_acc = "aria go away for 2 hours"
        await s._maybe_stop_phrase()
        await asyncio.sleep(0.02)  # let the callback task run

        self.assertTrue(s._silenced)                 # output gated
        self.assertTrue(s._audio_out_queue.empty())  # pending audio dropped -> silent
        self.assertEqual(fired, [7200.0])            # stop fired with the duration

    async def test_normal_speech_never_silences(self):
        import asyncio
        from src.gemini_session import GeminiSession

        fired: list[float] = []

        async def cb(secs: float) -> None:
            fired.append(secs)

        s = GeminiSession(stop_phrase_callback=cb)
        s._user_turn_acc = "what's on my calendar today"
        await s._maybe_stop_phrase()
        await asyncio.sleep(0.02)

        self.assertFalse(s._silenced)
        self.assertEqual(fired, [])


class TestVoiceGates(unittest.IsolatedAsyncioTestCase):
    """While away, the wake word opens no session and auto-join refuses — the
    root fix for the empty-room 3 AM self-talk."""

    async def test_wake_word_ignored_while_away(self):
        from src import bot

        with patch("src.presence.is_away", return_value=True), \
             patch.object(bot, "_local_session_active", False), \
             patch.object(bot, "gemini", MagicMock(connect=AsyncMock())) as gem:
            await bot._on_wake_word()
            gem.connect.assert_not_awaited()  # no voice session opened

    async def test_auto_join_refused_while_away(self):
        from src import bot

        with patch("src.presence.is_away", return_value=True):
            ok = await bot._auto_join_voice_channel(MagicMock())
            self.assertFalse(ok)

    async def test_wake_word_opens_session_when_present(self):
        from src import bot

        vc = MagicMock()
        vc.in_voice = False
        with patch("src.presence.is_away", return_value=False), \
             patch.object(bot, "_local_session_active", False), \
             patch.object(bot, "voice_controller", vc), \
             patch.object(bot, "_wake_listener", None), \
             patch.object(bot, "SpeakerOutput", MagicMock()), \
             patch.object(bot, "gemini", MagicMock(connected=False, connect=AsyncMock())) as gem:
            await bot._on_wake_word()
            gem.connect.assert_awaited()  # present => she wakes normally


if __name__ == "__main__":
    unittest.main(verbosity=2)
