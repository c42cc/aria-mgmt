"""Co-presence — deliver (efferent) + awareness (afferent).

The panther forensic (2026-06-25, session 1519565561149915258): asked to be SENT
a file in the chat, Aria had no way to hand one over and no ambient awareness of
what she'd made, so she blind-`find`-searched and deflected to "open it on the
Mac / iMessage / email". These lock the two primitives that close that — and the
discipline that delivery is never a silent failure or a fabricated "sent".
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

from src import tools


@pytest.fixture
def cfg(monkeypatch):
    """A throwaway config shim so tests control the upload cap + artifact dirs
    without mutating the frozen global Config."""
    ns = SimpleNamespace(discord_upload_limit_mb=25.0, artifact_dirs=[])
    monkeypatch.setattr(tools, "config", ns)
    tools._SURROUNDINGS_CACHE.update(ts=0.0, summary=None)
    return ns


async def _ok_cb(path, note, session_key):
    return {"delivered": True, "url": "https://cdn.discordapp.com/attachments/x/y.bin",
            "filename": os.path.basename(path), "bytes": os.path.getsize(path)}


async def _raise_cb(path, note, session_key):
    raise RuntimeError("thread gone")


# --- delivery: ground truth, typed blockers, never silent -------------------

def test_deliver_requires_path():
    out = json.loads(asyncio.run(tools._deliver(path="")))
    assert out["delivered"] is False and "path is required" in out["error"]


def test_deliver_missing_file(tmp_path):
    out = json.loads(asyncio.run(tools._deliver(path=str(tmp_path / "nope.mp4"))))
    assert out["delivered"] is False and "no file" in out["error"]


def test_deliver_unwired_is_loud(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"; f.write_text("hi")
    monkeypatch.setattr(tools, "_send_file_callback", None)
    out = json.loads(asyncio.run(tools._deliver(path=str(f))))
    assert out["delivered"] is False and "not wired" in out["error"]


def test_deliver_over_cap_is_typed_blocker(cfg, tmp_path, monkeypatch):
    cfg.discord_upload_limit_mb = 1
    f = tmp_path / "big.bin"; f.write_bytes(b"\0" * (3 * 1024 * 1024))
    monkeypatch.setattr(tools, "_send_file_callback", _ok_cb)
    out = json.loads(asyncio.run(tools._deliver(path=str(f))))
    # over-cap is a NAMED blocker with a fix — not a silent drop, not a fabricated send
    assert out["delivered"] is False and "blocker" in out and "fix" in out


def test_deliver_success_returns_discord_ground_truth(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"; f.write_text("hi")
    monkeypatch.setattr(tools, "_send_file_callback", _ok_cb)
    out = json.loads(asyncio.run(tools._deliver(path=str(f), note="here")))
    assert out["delivered"] is True and out["url"].startswith("https://")


def test_deliver_callback_failure_is_loud_not_silent(tmp_path, monkeypatch):
    f = tmp_path / "a.txt"; f.write_text("hi")
    monkeypatch.setattr(tools, "_send_file_callback", _raise_cb)
    out = json.loads(asyncio.run(tools._deliver(path=str(f))))
    # a Discord error surfaces as a typed failure the engine sees — never "sent"
    assert out["delivered"] is False and "delivery failed" in out["error"] and "thread gone" in out["error"]


# --- awareness: provenance-ranked, not a blind find -------------------------

def _seed_panther_candidates(tmp_path):
    # the real candidate set: a named lesson (right) vs a 21KB armature test (wrong)
    # vs a hash-named raw capture, vs an unrelated lesson.
    (tmp_path / "panther_life_birth_to_death_2026-06-23_bd0345d8.mp4").write_bytes(b"\0" * 1000)
    (tmp_path / "panther_armature_drawon.mp4").write_bytes(b"\0" * 100)
    (tmp_path / "4bc7729366934abc.video.mp4").write_bytes(b"\0" * 500)
    (tmp_path / "solar_system_lesson_2026-06-23.mp4").write_bytes(b"\0" * 900)
    now = time.time()
    os.utime(tmp_path / "panther_life_birth_to_death_2026-06-23_bd0345d8.mp4", (now, now))
    os.utime(tmp_path / "panther_armature_drawon.mp4", (now - 3600, now - 3600))


def test_recent_artifacts_ranks_named_lesson_over_test(cfg, tmp_path):
    _seed_panther_candidates(tmp_path)
    cfg.artifact_dirs = [str(tmp_path)]
    r = json.loads(asyncio.run(tools._recent_artifacts("panther video")))
    names = [a["name"] for a in r["artifacts"]]
    # the real lesson resolves first — NOT the 21KB armature test (the panther bug)
    assert names[0].startswith("panther_life_birth_to_death")
    assert names[0] != "panther_armature_drawon.mp4"
    # and it tells the engine to deliver, not to ask "which one?"
    assert "do not ask the user which one" in r["note"]


def test_surroundings_is_bounded_and_present(cfg, tmp_path):
    for i in range(12):
        (tmp_path / f"export_{i}_lesson.mp4").write_bytes(b"\0" * 10)
    cfg.artifact_dirs = [str(tmp_path)]
    s = tools.surroundings_summary(max_items=5)
    assert "Around you right now" in s
    assert s.count("\n- ") <= 5  # bounded — never the firehose


def test_surroundings_empty_when_nothing_around(cfg, tmp_path):
    cfg.artifact_dirs = [str(tmp_path)]
    assert tools.surroundings_summary() == ""


def test_scan_is_non_recursive(cfg, tmp_path):
    # a blind recursive find was the failure; the scan must NOT descend into nested
    # dirs (the hash-named session captures live nested and stay out of awareness).
    nested = tmp_path / "data" / "sessions" / "20260624"
    nested.mkdir(parents=True)
    (nested / "deadbeef.video.mp4").write_bytes(b"\0" * 10)
    (tmp_path / "named_lesson_export.mp4").write_bytes(b"\0" * 10)
    cfg.artifact_dirs = [str(tmp_path)]
    arts = tools._scan_recent_artifacts()
    names = [a["name"] for a in arts]
    assert "named_lesson_export.mp4" in names
    assert "deadbeef.video.mp4" not in names


# --- bytes-level artifact verifier: pure parts (no API) ---------------------

def test_solid_png_is_a_valid_png():
    from src import fulfillment as f
    png = f._solid_png((10, 20, 30), size=8)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in png and b"IDAT" in png and b"IEND" in png


def test_guess_mime():
    from src import fulfillment as f
    assert f._guess_mime("x.mp4") == "video/mp4"
    assert f._guess_mime("x.PNG") == "image/png"
    assert f._guess_mime("x.unknown") == "application/octet-stream"


def test_artifact_verify_is_loud_without_key(monkeypatch):
    from types import SimpleNamespace
    from src import fulfillment as f
    from src.judge import JudgeError
    monkeypatch.setattr(f, "config", SimpleNamespace(google_api_key=""))
    with pytest.raises(JudgeError):
        asyncio.run(f.verify_delivered_artifact(b"\x89PNG-fake", "anything", mime_type="image/png"))
