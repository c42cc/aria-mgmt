"""The routes primitive (forensic 2026-06-25, "FUCK YOU, I'm 100 miles away,
figure it out"): when IDE control (CDP) is down, the conductor must SENSE it at
turn-time and route around to the SAME intent — never hand the owner a fix-it
command as a stop. These tests prove the context carries that instruction when
CDP is down, and stays quiet when it is up.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class RoutesHealthInContext(unittest.TestCase):
    def test_cdp_down_surfaces_route_around_not_handback(self):
        from src import tools
        with patch("src.cursor_ide_driver.cdp_up", return_value=False):
            ctx = tools._build_context("S")
        self.assertIn("cursor IDE control: DOWN", ctx)
        # The same-intent guard: route around, do not hand back a command.
        self.assertIn("SAME intent", ctx)
        self.assertIn("Do NOT hand the user", ctx)
        # The manual fix is the LAST resort, still present for the no-route case.
        self.assertIn("cursor_ide_debug.sh", ctx)

    def test_cdp_up_is_silent(self):
        from src import tools
        with patch("src.cursor_ide_driver.cdp_up", return_value=True):
            ctx = tools._build_context("S")
        self.assertNotIn("cursor IDE control: DOWN", ctx)


class CdpHealthProbe(unittest.TestCase):
    def test_probe_caches_and_never_raises(self):
        import src.cursor_ide_driver as drv
        # Closed/refused port -> clean False, never an exception.
        drv._cdp_health_cache = (0.0, False)
        self.assertFalse(drv.cdp_up(port=1))  # port 1: connection refused
        # Cached: a second call within TTL returns the cached value without
        # re-probing (create_connection would blow up if called again).
        with patch("socket.create_connection", side_effect=AssertionError("re-probed")):
            self.assertFalse(drv.cdp_up(port=1))


if __name__ == "__main__":
    unittest.main()
