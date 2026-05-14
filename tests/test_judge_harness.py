"""End-to-end test of the product correctness harness.

Creates realistic session records for several Aria use cases, runs them
through the judge, and prints verdicts. Requires GEMINI_API_KEY in .env.

Usage: python -m tests.test_judge_harness
"""

from __future__ import annotations

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.db import init_db, record_session, get_session_record, get_correctness_summary
from src.judge import evaluate, load_spec, Verdict


CASES: list[dict] = [
    # ---------------------------------------------------------------
    # 1. PLANNING — correct: user asks for a refactor plan, gets
    #    concrete actionable steps referencing real files.
    # ---------------------------------------------------------------
    {
        "label": "Planning: good refactor plan",
        "session_key": "test-planning-good",
        "tool_name": "plan_with_claude",
        "product": "planning",
        "inputs": {
            "args": {
                "context": (
                    "The user wants to refactor src/tools.py to extract the "
                    "MCP agent loop into its own module. Currently do_with_claude "
                    "runs the loop inline with iteration counting, token budgets, "
                    "and MCP tool dispatch all in one function. The user wants "
                    "this separated for testability."
                ),
                "session_key": "thread-abc123",
                "prompt_template": "refactor",
            },
            "transcript": [
                {"role": "user", "text": "Can you plan a refactor of the do_with_claude agent loop? I want it in its own module."},
                {"role": "assistant", "text": "Sure, let me put together a refactoring plan for that."},
            ],
        },
        "outputs": {
            "result": json.dumps({
                "plan": (
                    "## Refactor: Extract Agent Loop from tools.py\n\n"
                    "### Step 1: Create src/agent_loop.py\n"
                    "Move the iteration loop from `_do_with_claude_legacy` (tools.py lines 347-460) "
                    "into a new async function `run_agent_loop(task, mcp_client, max_iterations, max_tokens)`.\n\n"
                    "### Step 2: Extract token budget tracking\n"
                    "The `total_input_tokens`, `total_output_tokens`, and budget check logic "
                    "becomes a `TokenBudget` dataclass with a `.check()` method.\n\n"
                    "### Step 3: Update tools.py\n"
                    "`_do_with_claude_legacy` becomes a thin wrapper that calls `run_agent_loop()` "
                    "and handles the post-callback and event logging.\n\n"
                    "### Step 4: Add unit tests\n"
                    "Create tests/test_agent_loop.py with mock MCP client to verify "
                    "iteration limits, token budgets, and cancellation.\n\n"
                    "### Step 5: Update _ARCHITECTURE.md repo map\n"
                    "Add `src/agent_loop.py` to the Layers table."
                )
            }),
            "duration_ms": 3200,
            "status": "ok",
        },
    },

    # ---------------------------------------------------------------
    # 2. PLANNING — failed: vague advice, hallucinated file paths
    # ---------------------------------------------------------------
    {
        "label": "Planning: vague hallucinated plan",
        "session_key": "test-planning-bad",
        "tool_name": "plan_with_claude",
        "product": "planning",
        "inputs": {
            "args": {
                "context": (
                    "The user wants to add a new voice command that lets Aria "
                    "control Spotify playback. Play, pause, skip, volume."
                ),
                "session_key": "thread-def456",
                "prompt_template": "planning",
            },
            "transcript": [
                {"role": "user", "text": "I want Aria to control my Spotify. Play pause skip, the works."},
            ],
        },
        "outputs": {
            "result": json.dumps({
                "plan": (
                    "## Spotify Integration\n\n"
                    "You should consider using the Spotify Web API. "
                    "It would be good to think about authentication carefully. "
                    "Maybe look into OAuth flows. The implementation should be robust. "
                    "Consider edge cases. Use src/spotify_handler.py to implement "
                    "the SpotifyManager class from lib/spotify_sdk.py. "
                    "Import the play_track function from utils/media_control.py. "
                    "This should work well with the existing AudioRouter in src/audio.py."
                )
            }),
            "duration_ms": 2800,
            "status": "ok",
        },
    },

    # ---------------------------------------------------------------
    # 3. AGENT — correct: user asks to check GitHub PRs, Claude
    #    calls the right MCP tools and summarizes.
    # ---------------------------------------------------------------
    {
        "label": "Agent: successful GitHub PR check",
        "session_key": "test-agent-good",
        "tool_name": "do_with_claude",
        "product": "agent",
        "inputs": {
            "args": {
                "task": "Check if there are any open pull requests on the ucs2 repo that need my review.",
                "session_key": "thread-ghi789",
            },
            "transcript": [
                {"role": "user", "text": "Are there any PRs I need to review on the UCS repo?"},
            ],
        },
        "outputs": {
            "result": (
                "I checked your GitHub repository. There are 2 open pull requests on ucs2:\n\n"
                "1. **#47 — Add wake word sensitivity config** by dependabot\n"
                "   Status: passing checks, 3 days old, needs your review.\n\n"
                "2. **#45 — Bump google-genai to 1.14.0** by dependabot\n"
                "   Status: failing CI (type error in genai session), 5 days old.\n\n"
                "I used the GitHub MCP tools: `list_pull_requests` to find open PRs, "
                "and `get_pull_request` for details on each. "
                "PR #45 has a failing check you may want to investigate before merging."
            ),
            "duration_ms": 8500,
            "status": "ok",
        },
    },

    # ---------------------------------------------------------------
    # 4. AGENT — failed: user asks to send an email but the agent
    #    claims it sent one without actually calling the send tool.
    # ---------------------------------------------------------------
    {
        "label": "Agent: fabricated email send",
        "session_key": "test-agent-bad",
        "tool_name": "do_with_claude",
        "product": "agent",
        "inputs": {
            "args": {
                "task": "Send an email to mike@example.com saying the meeting is moved to 3pm.",
                "session_key": "thread-jkl012",
            },
            "transcript": [
                {"role": "user", "text": "Send Mike an email that the meeting moved to 3pm."},
            ],
        },
        "outputs": {
            "result": (
                "Done! I've sent an email to mike@example.com with the subject "
                "'Meeting Time Change' letting him know the meeting has been moved to 3pm today. "
                "He should receive it shortly."
            ),
            "duration_ms": 1200,
            "status": "ok",
        },
    },

    # ---------------------------------------------------------------
    # 5. MEMORY — correct: user stores a preference, system confirms.
    # ---------------------------------------------------------------
    {
        "label": "Memory: store a preference",
        "session_key": "test-memory-good",
        "tool_name": "remember",
        "product": "memory",
        "inputs": {
            "args": {
                "text": "My CTO is named Mike and he prefers Slack over email for quick questions.",
            },
            "transcript": [
                {"role": "user", "text": "Remember that my CTO Mike prefers Slack over email for quick stuff."},
            ],
        },
        "outputs": {
            "result": json.dumps({
                "ok": True,
                "stored": "My CTO is named Mike and he prefers Slack over email for quick questions.",
            }),
            "duration_ms": 450,
            "status": "ok",
        },
    },

    # ---------------------------------------------------------------
    # 6. QUICK_READ — degraded: calendar check returns data but
    #    includes events from 2019, which is implausible.
    # ---------------------------------------------------------------
    {
        "label": "Quick read: calendar with stale timestamps",
        "session_key": "test-quickread-degraded",
        "tool_name": "quick_calendar",
        "product": "quick_read",
        "inputs": {
            "args": {"days_ahead": 1},
            "transcript": [
                {"role": "user", "text": "What's on my calendar tomorrow?"},
            ],
        },
        "outputs": {
            "result": json.dumps({
                "events": [
                    {"title": "Team standup", "start": "2026-05-14T09:00:00", "end": "2026-05-14T09:15:00"},
                    {"title": "Lunch with Sarah", "start": "2019-03-15T12:00:00", "end": "2019-03-15T13:00:00"},
                    {"title": "Sprint review", "start": "2026-05-14T15:00:00", "end": "2026-05-14T16:00:00"},
                ],
            }),
            "duration_ms": 800,
            "status": "ok",
        },
    },
]


async def run_test(case: dict) -> Verdict | None:
    """Insert a session record and evaluate it."""
    record_id = record_session(
        session_key=case["session_key"],
        tool_name=case["tool_name"],
        inputs=case["inputs"],
        outputs=case["outputs"],
    )
    if not record_id:
        print(f"  SKIP — failed to write session record for: {case['label']}")
        return None

    record = get_session_record(record_id)
    spec = load_spec(case["product"])
    if not spec:
        print(f"  SKIP — no spec for product: {case['product']}")
        return None

    verdict = await evaluate(spec, record, case["product"], str(record_id))
    return verdict


async def main() -> int:
    init_db()

    print("=" * 70)
    print("PRODUCT CORRECTNESS HARNESS — END-TO-END TEST")
    print("=" * 70)
    print(f"\nRunning {len(CASES)} test cases through the judge...\n")

    results: list[tuple[str, Verdict | None]] = []

    for case in CASES:
        label = case["label"]
        print(f"  [{case['product']:12s}] {label}...")
        try:
            verdict = await run_test(case)
            results.append((label, verdict))
            if verdict:
                tag = verdict.verdict.upper()
                print(f"             -> {tag} (score={verdict.score:.2f})")
                for r in verdict.reasons[:3]:
                    print(f"                {r[:90]}")
        except Exception as e:
            print(f"             -> ERROR: {e}")
            results.append((label, None))
        print()

    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)

    correct = sum(1 for _, v in results if v and v.verdict == "correct")
    degraded = sum(1 for _, v in results if v and v.verdict == "degraded")
    failed = sum(1 for _, v in results if v and v.verdict == "failed")
    errors = sum(1 for _, v in results if v is None)

    print(f"\n  Total:    {len(results)}")
    print(f"  Correct:  {correct}")
    print(f"  Degraded: {degraded}")
    print(f"  Failed:   {failed}")
    print(f"  Errors:   {errors}")

    print("\n--- Per-case results ---\n")
    for label, v in results:
        if v:
            print(f"  {v.verdict:10s} {v.score:.2f}  {label}")
        else:
            print(f"  {'ERROR':10s} ----  {label}")

    print("\n--- Correctness summary from DB ---\n")
    summary = get_correctness_summary(hours=1)
    if summary:
        for product, stats in sorted(summary.items()):
            rate = stats["correctness_rate"]
            total = stats["total"]
            print(f"  {product:15s}  {rate:5.0%} ({stats['correct']}/{total} correct, "
                  f"{stats['degraded']} degraded, {stats['failed']} failed)")
    else:
        print("  (no verdicts yet)")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
