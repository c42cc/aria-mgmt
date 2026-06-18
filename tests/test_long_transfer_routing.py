"""Step 3 lock: a timeout on an inherently-long transfer routes to the background
runner, it is NOT a transient blip retried into the same wall.

This is the 21GB-clone grind the post-mortem named: a `git clone` / `git lfs
pull` of a model timed out through the shell tool, got classified TRANSIENT,
retried once, then surfaced a false "Blocked: a stable connection" — when the
real cause was that the transfer can't fit a timeout-bounded shell at all.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.outcomes import (  # noqa: E402
    BLOCKED,
    PROGRESS,
    TRANSIENT,
    _LONG_JOB_NEED,
    classify_outcome,
)


class LongTransferRouting(unittest.TestCase):
    def test_git_clone_timeout_routes_to_background(self):
        out = classify_outcome(
            "execute_command",
            {"command": "git clone https://huggingface.co/GD-ML/DreamX-World-5B"},
            "Command execution timed out",
        )
        self.assertEqual(out.kind, BLOCKED)
        self.assertEqual(out.need, _LONG_JOB_NEED)

    def test_git_lfs_pull_timeout_routes_to_background(self):
        out = classify_outcome(
            "execute_command",
            {"command": "cd model && git lfs pull"},
            json.dumps({"exitCode": 1, "stderr": "fatal: the operation timed out"}),
        )
        self.assertEqual(out.kind, BLOCKED)
        self.assertEqual(out.need, _LONG_JOB_NEED)

    def test_short_curl_timeout_is_still_transient(self):
        # A timeout on a NON-transfer command (a tiny API curl) is a genuine
        # blip and must stay TRANSIENT — the long-transfer routing must not
        # swallow every timeout.
        out = classify_outcome(
            "execute_command",
            {"command": "curl https://api.example.com/v1/ping"},
            json.dumps({"exitCode": 1, "stderr": "curl: (28) Connection timed out"}),
        )
        self.assertEqual(out.kind, TRANSIENT)

    def test_successful_clone_is_progress_not_misrouted(self):
        # The command being a long transfer is not enough — only a TIMEOUT routes.
        out = classify_outcome(
            "execute_command",
            {"command": "git clone https://huggingface.co/org/tiny"},
            "Cloning into 'tiny'... done. 42 files.",
        )
        self.assertEqual(out.kind, PROGRESS)


if __name__ == "__main__":
    unittest.main()
