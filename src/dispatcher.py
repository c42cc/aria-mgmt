"""The dispatcher — fill the loop template, run the engine, verify against ground truth.

The go-gate is upstream (the bot only calls this after a confirmed, explicit go).
Here we route by the loop's endpoint, build the instruction from the loop's
dispatch template + the doctrine (so it reaches the engine — review 3.6), run the
engine, then check "done" against GROUND TRUTH — never the engine's own narration.

Two endpoints today:
- mac-claude-code: full build power; verified by git diff + an independent test run.
- research: web/read tools ONLY (no shell), so untrusted web content can never
  prompt-inject the executor into running a command (review 3.8); verified by a
  substantial brief returned without error.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from . import engine_claude_code, projects, prompts
from .config import config
from .loops import Loop

# The research whitelist — the untrusted-content boundary. No Bash, no Edit.
_RESEARCH_TOOLS = ["WebSearch", "WebFetch", "Read", "Glob", "Grep"]


@dataclass
class DispatchResult:
    delivered: bool
    summary: str
    broke: str | None
    diff_stat: str
    tests_passed: bool | None
    cost_usd: float
    session_id: str


def _git(ws: str, *args: str) -> tuple[int, str]:
    p = subprocess.run(["git", "-C", ws, *args], capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def _build_instruction(loop: Loop, slots: dict, workspace_root: str | None) -> str:
    doctrine = prompts.load("_principles")
    keys = [s.key for s in (*loop.required_slots, *loop.optional_slots)]
    filled = {k: (str(slots.get(k)).strip() if slots.get(k) else "none specified") for k in keys}
    if "repo" in filled and workspace_root:
        filled["repo"] = workspace_root
    body = loop.dispatch.format(**filled)
    return (
        "[Operating doctrine you must honor — these are not suggestions]\n"
        f"{doctrine}\n\n[Task]\n{body}\n"
    )


def _run_tests(ws: str) -> tuple[bool | None, str]:
    """Independent ground-truth test run. None = could not verify (a loud
    non-pass, never a crash and never a false FAIL)."""
    import glob
    import os
    import sys

    has_pytests = bool(glob.glob(os.path.join(ws, "**", "test_*.py"), recursive=True)) or bool(
        glob.glob(os.path.join(ws, "**", "*_test.py"), recursive=True)
    )
    if not has_pytests:
        return (None, "no known test command (tests unverified)")
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ws, capture_output=True, text=True)
    except FileNotFoundError as e:
        return (None, f"could not launch the test runner: {e}")
    out = (p.stdout + p.stderr).strip()
    if "No module named pytest" in out:
        return (None, "pytest is not installed in the verifier interpreter")
    if p.returncode == 5:
        return (None, "pytest collected no tests")
    return (p.returncode == 0, out[-2000:])


def run(loop: Loop, slots: dict) -> DispatchResult:
    """Dispatch a confirmed loop to its endpoint and verify the outcome."""
    if loop.endpoint == "mac-claude-code":
        return _run_build(loop, slots)
    if loop.endpoint == "research":
        return _run_research(loop, slots)
    return DispatchResult(False, "", f"endpoint {loop.endpoint!r} is not wired yet", "", None, 0.0, "")


def _run_build(loop: Loop, slots: dict) -> DispatchResult:
    repo_name = (slots.get("repo") or "").strip()
    ws = projects.resolve(repo_name)
    if not ws:
        return DispatchResult(
            False, "", f"I don't know the project {repo_name!r} — add it to projects/registry.md or give an absolute path.",
            "", None, 0.0, "",
        )

    before_head = _git(ws, "rev-parse", "HEAD")[1]
    before_branch = _git(ws, "rev-parse", "--abbrev-ref", "HEAD")[1]

    instruction = _build_instruction(loop, slots, ws)
    t0 = time.time()
    result = engine_claude_code.run(ws, instruction)
    elapsed = time.time() - t0

    after_head = _git(ws, "rev-parse", "HEAD")[1]
    after_branch = _git(ws, "rev-parse", "--abbrev-ref", "HEAD")[1]
    dirty = _git(ws, "status", "--porcelain")[1]
    diff_stat = _git(ws, "diff", "--stat", before_head)[1] if before_head else ""
    diff_exists = bool(dirty) or (after_head and after_head != before_head) or (after_branch != before_branch)

    tests_passed, tests_detail = _run_tests(ws)

    if result.error:
        broke, delivered = f"engine error: {result.error}", False
    elif not diff_exists:
        broke, delivered = "the engine finished but left no diff — nothing changed", False
    elif tests_passed is False:
        broke = f"a diff exists but tests FAIL: {tests_detail.splitlines()[-1] if tests_detail else ''}"
        delivered = False
    else:
        broke, delivered = None, True

    summary = (
        f"branch {after_branch} (from {before_branch}); diff: {diff_stat or '(none)'}; "
        f"tests: {'pass' if tests_passed else ('FAIL' if tests_passed is False else 'unverified')}; "
        f"{elapsed:.0f}s. engine said: {result.text[-400:]}"
    )
    return DispatchResult(delivered, summary, broke, diff_stat, tests_passed, result.cost_usd, result.session_id)


def _run_research(loop: Loop, slots: dict) -> DispatchResult:
    """Research endpoint: web/read tools ONLY. The whitelist IS the boundary —
    a hostile page cannot prompt-inject the executor into a shell it doesn't have."""
    ws = config.data_dir
    ws.mkdir(parents=True, exist_ok=True)
    instruction = _build_instruction(loop, slots, None)
    t0 = time.time()
    result = engine_claude_code.run(str(ws), instruction, allowed_tools=_RESEARCH_TOOLS)
    elapsed = time.time() - t0

    brief = (result.text or "").strip()
    if result.error:
        broke, delivered = f"engine error: {result.error}", False
    elif len(brief) < 200:
        broke, delivered = f"no substantial brief returned (got {len(brief)} chars)", False
    else:
        broke, delivered = None, True

    summary = f"{elapsed:.0f}s; brief {len(brief)} chars. {brief[:400]}"
    return DispatchResult(delivered, summary, broke, "", None, result.cost_usd, result.session_id)
