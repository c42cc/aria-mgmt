"""The dispatcher — fill the loop template, run the engine, verify against ground truth.

The go-gate is upstream (the bot only calls this after a confirmed, explicit go).
Here we: resolve the endpoint/repo, build the instruction from the loop's dispatch
template + the doctrine (so it reaches the engine — review 3.6), run the engine,
then check "done" against GROUND TRUTH — the git diff and the test exit code —
never the engine's own narration (recombining the verifier with the producer).
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from . import engine_claude_code, projects, prompts
from .loops import Loop


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


def _build_instruction(loop: Loop, workspace_root: str, slots: dict) -> str:
    doctrine = prompts.load("_principles")
    keys = [s.key for s in (*loop.required_slots, *loop.optional_slots)]
    filled = {k: (str(slots.get(k)).strip() if slots.get(k) else "none specified") for k in keys}
    filled["repo"] = workspace_root
    body = loop.dispatch.format(**filled)
    return (
        "[Operating doctrine you must honor — these are not suggestions]\n"
        f"{doctrine}\n\n"
        "[Task]\n"
        f"{body}\n"
    )


def _run_tests(ws: str) -> tuple[bool | None, str]:
    """Independent ground-truth test run. None = could not verify (unverified is
    a loud non-pass, never a crash and never a false FAIL — a broken verifier
    must not read as a failed build)."""
    import glob
    import os
    import sys

    has_pytests = bool(glob.glob(os.path.join(ws, "**", "test_*.py"), recursive=True)) or bool(
        glob.glob(os.path.join(ws, "**", "*_test.py"), recursive=True)
    )
    if not has_pytests:
        return (None, "no known test command (tests unverified)")
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"], cwd=ws, capture_output=True, text=True
        )
    except FileNotFoundError as e:
        return (None, f"could not launch the test runner: {e}")
    out = (p.stdout + p.stderr).strip()
    if "No module named pytest" in out:
        return (None, "pytest is not installed in the verifier interpreter")
    if p.returncode == 5:
        return (None, "pytest collected no tests")
    return (p.returncode == 0, out[-2000:])


def run(loop: Loop, slots: dict) -> DispatchResult:
    """Dispatch a confirmed loop to its engine and verify the outcome."""
    if loop.endpoint != "mac-claude-code":
        return DispatchResult(False, "", f"endpoint {loop.endpoint!r} is not wired yet", "", None, 0.0, "")

    repo_name = (slots.get("repo") or "").strip()
    ws = projects.resolve(repo_name)
    if not ws:
        return DispatchResult(
            False, "", f"I don't know the project {repo_name!r} — add it to projects/registry.md or give an absolute path.",
            "", None, 0.0, "",
        )

    before_head = _git(ws, "rev-parse", "HEAD")[1]
    before_branch = _git(ws, "rev-parse", "--abbrev-ref", "HEAD")[1]

    instruction = _build_instruction(loop, ws, slots)
    t0 = time.time()
    result = engine_claude_code.run(ws, instruction)
    elapsed = time.time() - t0

    # Ground truth: did anything actually change?
    after_head = _git(ws, "rev-parse", "HEAD")[1]
    after_branch = _git(ws, "rev-parse", "--abbrev-ref", "HEAD")[1]
    dirty = _git(ws, "status", "--porcelain")[1]
    diff_stat = _git(ws, "diff", "--stat", before_head)[1] if before_head else ""
    diff_exists = bool(dirty) or (after_head and after_head != before_head) or (after_branch != before_branch)

    tests_passed, tests_detail = _run_tests(ws)

    if result.error:
        broke = f"engine error: {result.error}"
        delivered = False
    elif not diff_exists:
        broke = "the engine finished but left no diff — nothing changed"
        delivered = False
    elif tests_passed is False:
        broke = f"a diff exists but tests FAIL: {tests_detail.splitlines()[-1] if tests_detail else ''}"
        delivered = False
    else:
        broke = None
        delivered = True

    summary = (
        f"branch {after_branch} (from {before_branch}); diff: {diff_stat or '(none)'}; "
        f"tests: {'pass' if tests_passed else ('FAIL' if tests_passed is False else 'unverified')}; "
        f"{elapsed:.0f}s. engine said: {result.text[-400:]}"
    )
    return DispatchResult(
        delivered=delivered,
        summary=summary,
        broke=broke,
        diff_stat=diff_stat,
        tests_passed=tests_passed,
        cost_usd=result.cost_usd,
        session_id=result.session_id,
    )
