"""Test isolation — never write to the real data/ (telemetry, outcomes, memory).

The trace/outcome/memory paths are properties off config.data_dir, so redirecting
data_dir to a per-test tmp dir isolates every filesystem side effect. Without
this, stub-conductor test turns leak ~0ms latencies into the real experience
telemetry and corrupt the measurement loop (review 2.6).
"""

from __future__ import annotations

import pytest

from src.config import config


@pytest.fixture(autouse=True)
def _isolate_data(tmp_path):
    original = config.data_dir
    object.__setattr__(config, "data_dir", tmp_path)  # frozen dataclass: deliberate test override
    try:
        yield
    finally:
        object.__setattr__(config, "data_dir", original)
