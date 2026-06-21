"""Phase 0 unit suite — no API, no network. The static half of the gate."""

from __future__ import annotations

import types

import pytest

from src import conductor, loops, projects, prompts


def test_feature_build_loop_loads():
    lp = loops.load_loops()
    assert "feature-build" in lp
    assert lp["feature-build"].required_keys() == ["repo", "change", "acceptance"]
    assert lp["feature-build"].endpoint == "mac-claude-code"


def test_conductor_prompt_resolves_principles():
    txt = prompts.load("conductor")
    # the doctrine must be injected by construction, not pasted
    assert "dysfunctional primitive" in txt
    assert "{{include:" not in txt  # fully resolved


def test_missing_include_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "config", types.SimpleNamespace(prompts_dir=tmp_path))
    (tmp_path / "broken.md").write_text("hello {{include:does_not_exist}}")
    with pytest.raises(FileNotFoundError):
        prompts.load("broken")


def test_include_cycle_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "config", types.SimpleNamespace(prompts_dir=tmp_path))
    (tmp_path / "a.md").write_text("{{include:b}}")
    (tmp_path / "b.md").write_text("{{include:a}}")
    with pytest.raises(ValueError):
        prompts.load("a")


def test_projects_resolve():
    assert projects.resolve("ucs")
    assert projects.resolve("nonexistent-project-xyz") is None
    assert projects.resolve("/tmp") == "/tmp"


def test_render_loops_lists_required():
    rendered = conductor._render_loops(loops.load_loops())
    assert "feature-build" in rendered
    assert "repo" in rendered and "acceptance" in rendered


def test_loop_phases_contract():
    assert conductor.PHASES == ("CHITCHAT", "INTERVIEW", "CONFIRM", "DISPATCH", "REPORT")
