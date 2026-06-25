"""The doctor must assemble every plane and render an honest single pane.

Network-free: the live probes are exercised by `make doctor`; here we lock the
assembly + rendering contract and the pure probes.
"""

from __future__ import annotations

from src import doctor


def test_floor_probe_is_absent_without_nas():
    p = doctor._probe_floor()
    assert p.name == "floor"
    assert p.state == "absent"
    assert "ABSENT" in p.detail


def test_cloud_probe_reports_key_present():
    p = doctor._probe_cloud()
    assert p.name == "cloud"
    assert p.state in ("ok", "not_configured")


def test_probe_assembles_all_planes(monkeypatch):
    # Stub the live probes so the test is deterministic + offline.
    monkeypatch.setattr(doctor, "_probe_mind", lambda: doctor.Plane("mind", "down", "x", "y"))
    monkeypatch.setattr(doctor, "_probe_hands", lambda: doctor.Plane("hands", "ok", "x"))
    monkeypatch.setattr(doctor, "_probe_ha", lambda: doctor.Plane("ha", "not_configured", "x"))
    report = doctor.probe()
    assert set(report["planes"]) == {"mind", "hands", "floor", "ha", "cloud"}
    assert "spend_today_usd" in report and "last_request" in report
    text = doctor.render(report)
    for name in ("mind", "hands", "floor", "ha", "cloud"):
        assert name in text


def test_glyphs_distinguish_states():
    assert doctor.Plane("x", "ok", "").glyph != doctor.Plane("x", "down", "").glyph
    assert doctor.Plane("x", "absent", "").glyph != doctor.Plane("x", "ok", "").glyph
