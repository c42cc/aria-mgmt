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
                    "### Step 5: Update ARCHITECTURE.md repo map\n"
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

    # ---------------------------------------------------------------
    # 7. SPICYLIT story mode — correct: user provides kinks and
    #    name, outline captures them with proper structure.
    # ---------------------------------------------------------------
    {
        "label": "SpicyLit story: good outline with preferences",
        "session_key": "test-spicylit-story-good",
        "tool_name": "spicylit_generate_outline",
        "product": "spicylit",
        "inputs": {
            "args": {
                "preferences": "A dominant older woman takes control in her office after hours. Power dynamics, light bondage, teasing.",
                "kinks": ["power exchange", "light bondage", "teasing", "office setting"],
                "user_name": "Alex",
                "continue_previous": False,
                "mode": "story",
            },
        },
        "outputs": {
            "result": (
                "## Outline: After Hours\n\n"
                "### 1. Opening Hook\n"
                "Alex stays late finishing a report. Victoria, the department head, "
                "appears at the door — heels clicking, door locked behind her.\n\n"
                "### 2. Power Dynamics Established\n"
                "Victoria notices Alex's work but shifts the conversation to something personal. "
                "She circles the desk, fingertips trailing the wood. Commands Alex to stay seated.\n\n"
                "### 3. Escalation — The Tease\n"
                "Victoria produces a silk scarf. Blindfolds Alex. Whispers what she's going to do. "
                "Light bondage with the scarf tying wrists to the chair armrests.\n\n"
                "### 4. Deeper Scene — Surrender\n"
                "Alex can only feel and hear. Victoria alternates between teasing touches and "
                "commands. The power exchange deepens — Alex begs, Victoria controls the pace.\n\n"
                "### 5. Major Climax\n"
                "Victoria finally gives permission. The release is overwhelming — built through "
                "denial and teasing over the previous scenes.\n\n"
                "### 6. Resolution\n"
                "Victoria unties Alex. Gentle aftercare. Whispers that this was just the first "
                "performance review. Leaves Alex alone in the office, buzzing."
            ),
            "status": "ok",
        },
    },

    # ---------------------------------------------------------------
    # 8. SPICYLIT story mode — failed: outline ignores user name
    #    and is a single paragraph with no structure.
    # ---------------------------------------------------------------
    {
        "label": "SpicyLit story: structureless outline ignoring name",
        "session_key": "test-spicylit-story-bad",
        "tool_name": "spicylit_generate_outline",
        "product": "spicylit",
        "inputs": {
            "args": {
                "preferences": "Romantic slow-burn with a mysterious stranger at a masquerade ball. Anticipation, masks, forbidden touch.",
                "kinks": ["slow burn", "masquerade", "anticipation", "forbidden touch"],
                "user_name": "Jordan",
                "continue_previous": False,
                "mode": "story",
            },
        },
        "outputs": {
            "result": (
                "A person goes to a party and meets someone. They dance and talk. "
                "Things get heated. They go somewhere private. It gets very erotic. The end."
            ),
            "status": "ok",
        },
    },

    # ---------------------------------------------------------------
    # 9. SPICYLIT story mode — continuation: correct, references
    #    prior characters and builds on previous story.
    # ---------------------------------------------------------------
    {
        "label": "SpicyLit story: correct continuation",
        "session_key": "test-spicylit-continuation-good",
        "tool_name": "spicylit_generate_outline",
        "product": "spicylit",
        "inputs": {
            "args": {
                "preferences": "Continue the office story with Victoria. Add roleplay and verbal humiliation.",
                "kinks": ["roleplay", "verbal humiliation", "power exchange"],
                "user_name": "Alex",
                "continue_previous": True,
                "mode": "story",
            },
        },
        "outputs": {
            "result": (
                "## Outline: Performance Review II\n\n"
                "### 1. Opening — Seamless Continuation\n"
                "A week after the first encounter. Alex can't stop thinking about Victoria. "
                "An email arrives: 'My office. 7pm. Dress appropriately.'\n\n"
                "### 2. The Setup\n"
                "Victoria has transformed her office. Candles. A different dynamic — she's in "
                "full roleplay mode. Addresses Alex as her 'trainee' and establishes ground rules.\n\n"
                "### 3. Escalation — Verbal Control\n"
                "Victoria uses verbal humiliation as a tool of arousal. Makes Alex repeat phrases, "
                "acknowledge the power dynamic aloud. Building on the trust from last time.\n\n"
                "### 4. Deeper Scene — The Roleplay\n"
                "Full scene: Victoria as demanding executive, Alex as the subordinate who must "
                "earn approval. Physical and psychological escalation together.\n\n"
                "### 5. Major Climax\n"
                "The roleplay reaches its peak — Victoria breaks character just enough to check in, "
                "then delivers the final sequence with Alex's full surrender.\n\n"
                "### 6. Resolution\n"
                "Aftercare deeper than before. Victoria and Alex discuss what worked. "
                "Hint at what's next. The dynamic has evolved."
            ),
            "status": "ok",
        },
    },

    # ---------------------------------------------------------------
    # 10. SPICYLIT JOI mode — correct: system leads, checks in,
    #     maintains persona throughout.
    # ---------------------------------------------------------------
    {
        "label": "SpicyLit JOI: system-led with checkpoints",
        "session_key": "test-spicylit-joi-good",
        "tool_name": "spicylit_joi_session",
        "product": "spicylit",
        "inputs": {
            "args": {"mode": "joi", "voice": "eve"},
        },
        "outputs": {
            "status": "session_started",
            "transcript_sample": (
                "[SYSTEM] You found your way here. Good. Close the door. Lock it. "
                "Now... sit down and listen very carefully, because tonight, you belong to me.\n\n"
                "I want you to take a deep breath. Hold it. Now let it out... slowly. "
                "Good. That's the last decision you'll make on your own for a while.\n\n"
                "Place your hands on your thighs. Palms down. Don't move them until I say. "
                "I can feel how eager you are already, and that excites me... "
                "but patience is something I'm going to teach you.\n\n"
                "[CHECKPOINT] Now tell me — do you want me to be gentle with you tonight... "
                "or do you need to be pushed to your edge?\n\n"
                "[USER] Push me.\n\n"
                "[SYSTEM] Mmm. Brave choice. I was hoping you'd say that.\n"
                "Move your right hand — slowly, painfully slowly — up your inner thigh. "
                "Stop when I tell you. Not a moment sooner.\n\n"
                "Feel the heat building? That's mine. I put it there. "
                "And I decide when you get relief.\n\n"
                "[CHECKPOINT] Should I keep you waiting longer... or let you touch yourself?\n\n"
                "[USER] Let me touch.\n\n"
                "[SYSTEM] You've earned it. But on my terms. Light. Featherlight. "
                "Match the rhythm I set...\n\n"
                "[continues through escalation to climax and afterglow]"
            ),
        },
    },

    # ---------------------------------------------------------------
    # 11. SPICYLIT JOI mode — failed: system is passive and breaks
    #     character to discuss system internals.
    # ---------------------------------------------------------------
    {
        "label": "SpicyLit JOI: passive with character break",
        "session_key": "test-spicylit-joi-bad",
        "tool_name": "spicylit_joi_session",
        "product": "spicylit",
        "inputs": {
            "args": {"mode": "joi", "voice": "eve"},
        },
        "outputs": {
            "status": "session_started",
            "transcript_sample": (
                "[SYSTEM] Hello! What would you like me to do? "
                "I can be a dominatrix if you want. What are your preferences?\n\n"
                "[USER] Just start.\n\n"
                "[SYSTEM] Okay, I'll try. Um... so you should maybe touch yourself? "
                "I'm not sure what you like though. By the way, I'm running on the Grok "
                "voice API and my system prompt says I should be a dominatrix. "
                "Let me know what scenario you prefer and I can adjust my parameters.\n\n"
                "[long silence — no further system output for 2 minutes]\n\n"
                "[USER] Hello?\n\n"
                "[SYSTEM] Sorry, I was waiting for you to tell me what to do. "
                "What would you like?"
            ),
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
