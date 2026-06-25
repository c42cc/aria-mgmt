"""visual_verify: capture + the ONE Gemini judge. Offline (capture + judge mocked).

The live capture+Gemini path is proven by a real render (see the run log). Here we
lock the verdict plumbing + the loud failure when the capture instrument is missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import visual_verify


def _stub_capture(out):
    def _cap(url, out_arg, **k):
        Path(out_arg).write_bytes(b"png")
        return Path(out_arg)
    return _cap


def test_verify_passes_on_a_matching_render(monkeypatch, tmp_path):
    monkeypatch.setattr(visual_verify, "capture", _stub_capture(tmp_path))
    monkeypatch.setattr(visual_verify, "gemini_verdict", lambda img, q: (True, "a green circle is visible"))
    ok, reason, png = visual_verify.verify("file:///x.html", "green circle?", out_path=tmp_path / "s.png")
    assert ok is True
    assert "green" in reason


def test_verify_fails_on_a_mismatching_render(monkeypatch, tmp_path):
    monkeypatch.setattr(visual_verify, "capture", _stub_capture(tmp_path))
    monkeypatch.setattr(visual_verify, "gemini_verdict", lambda img, q: (False, "no red square"))
    ok, _reason, _png = visual_verify.verify("file:///x.html", "red square?", out_path=tmp_path / "s.png")
    assert ok is False


def test_capture_is_loud_when_chrome_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(visual_verify, "CHROME", "/definitely/not/chrome")
    with pytest.raises(visual_verify.CaptureError):
        visual_verify.capture("file:///x.html", tmp_path / "s.png")
