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

from . import engine_claude_code, homeassistant, projects, prompts
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
    if loop.endpoint == "home-assistant":
        return _run_home(loop, slots)
    if loop.endpoint == "spark":
        return _run_spark(loop, slots)
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


def _run_home(loop: Loop, slots: dict) -> DispatchResult:
    """The house endpoint. The conductor has turned speech into (device, action);
    here we actuate Home Assistant DETERMINISTICALLY and verify against ground
    truth (re-read the entity state) — never the model's narration, never a cloud
    fallback. Free + fast (no model spend on the actuation hot path)."""
    target = str(slots.get("device") or slots.get("entity") or slots.get("area") or "").strip()
    if loop.id == "home-status":
        r = homeassistant.read_status(target)
    else:
        r = homeassistant.actuate(target, str(slots.get("action") or ""), slots.get("value"))
    return DispatchResult(r.delivered, r.summary, r.broke, "", None, 0.0, "")


def _run_spark(loop: Loop, slots: dict) -> DispatchResult:
    """The Spark endpoint — a local open-source model served on the DGX Spark runs
    the work (the Spark returns 'as an endpoint, never as core', ABSENCES.md). vLLM
    serves the Anthropic Messages API natively, so the SDK drives it unchanged via
    base_url. Local + private; cost is 0 (off the metered cloud)."""
    if not config.spark_base_url:
        return DispatchResult(
            False, "",
            "the Spark endpoint isn't configured — serve a model on the Spark and set "
            "SPARK_BASE_URL (and SPARK_MODEL) to its vLLM /v1 endpoint. No cloud fallback.",
            "", None, 0.0, "",
        )
    import anthropic

    # Plain filled dispatch — no engineering doctrine prefix (this is a general
    # text model doing a draft, not the build engine).
    keys = [s.key for s in (*loop.required_slots, *loop.optional_slots)]
    filled = {k: (str(slots.get(k)).strip() if slots.get(k) else "") for k in keys}
    instruction = loop.dispatch.format(**filled).strip()
    client = anthropic.Anthropic(
        base_url=config.spark_base_url,
        api_key=config.anthropic_api_key or "local-brain",
        timeout=config.anthropic_timeout_sec,
    )
    t0 = time.time()
    try:
        resp = client.messages.create(
            model=config.spark_model,
            max_tokens=config.spark_max_tokens,
            messages=[{"role": "user", "content": instruction}],
        )
    except Exception as e:  # loud + ours to fix; name the one thing to check
        return DispatchResult(
            False, "",
            f"the Spark model didn't answer ({type(e).__name__}: {e}) — is vLLM serving "
            f"{config.spark_model!r} at {config.spark_base_url}?",
            "", None, 0.0, "",
        )
    elapsed = time.time() - t0
    text = "".join(
        getattr(b, "text", "") for b in (resp.content or []) if getattr(b, "type", "") == "text"
    ).strip()
    if not text:
        broke, delivered = "the Spark model returned an empty response", False
    else:
        broke, delivered = None, True
    summary = f"{elapsed:.0f}s; {len(text)} chars from {config.spark_model}. {text[:400]}"
    return DispatchResult(delivered, summary, broke, "", None, 0.0, getattr(resp, "id", ""))
