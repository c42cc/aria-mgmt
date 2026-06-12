# Forensic: the $11.70 honeycomb question — the agent loop has no ground

**Date:** 2026-06-12 (audit of the `#How do we still have honeycomb running on
live visuals three?` thread, 2026-06-11 10:28 PT, and the morning-after
sessions)
**Scope:** sessions 162–171 across `data/state.db::loop_executions`,
`session_records`, `verdicts`, `events` (per-iteration costs), and
`data/audit.jsonl`.
**Follow-on to:** [`aria-threads.md`](aria-threads.md) (thread-per-request)
and [`aria-progress-governor.md`](aria-progress-governor.md) (outcome
classifier). Both of those fixes were live and **correctly did not fire** —
every command in this incident *succeeded*. This audit is the next layer
down: what happens when the loop is healthy but blind.

## Headline

| | |
|---|---|
| The request | "How do we still have honeycomb running on live visuals three?" |
| Honest price of the answer | one grep + three file reads ≈ **$0.50** |
| What it cost | **$11.70**, 17 Opus iterations, 26 MCP commands, 2 budget events, 1 apology |
| Run 1 (loop 224) | 7 iters, 390K input tokens, **$6.00 on a $5.00 cap**, answer found *and withheld* |
| User pushback | "How did the fuck does it cost six dollars to answer that question?" |
| Run 2 (loop 226) | re-did run 1's discovery from scratch: 9 iters, 340K tokens, **$5.20** |
| Per-iteration cost curve | $0.49 → $1.17 (run 1) — context snowball at full price |
| Next morning (session 170) | "mark the plan's to-do list": 11 blind-search iters, $5.26, nothing delivered, judged 0.0 |
| Night before (168/169) | bare fingerprint → clarifying question → judged FAILED by spec; "dont do anything w that" → new thread couldn't resolve "that" |

## The dysfunctional primitive

> **The agent loop starts blind. Nothing in the system binds the user's
> referents ("live visuals three", "the plan", "that") to artifacts, so every
> loop re-buys world knowledge at Opus prices — and throws it away at exit.**

Five mechanisms, one absence:

1. **`projects/registry.md` existed but was incomplete and never surfaced.**
   It mapped names → paths for exactly the purpose at hand, but
   `live_visuals_3` wasn't in it and `_build_context` never rendered it —
   only `build_with_cursor` read it. The loop's step 2 was therefore
   `search_files **/live_visuals_3*` over ALL of `/Users/corbin` (~110s, a
   huge result billed into context forever).
2. **Findings died at the wall.** Run 1 hit the $5 cap *after reading the
   answer's files* and returned only "Paused — hit its $5.00 budget". Run 2
   started from zero (wrong-path guess → `DIR_NOT_FOUND` → re-find →
   re-grep → re-read): the user paid twice for one discovery.
3. **Context economics were maximal.** No prompt caching anywhere (the
   ~20K-token static prefix re-billed at full price every iteration ≈ $3.20
   of run 1 alone), no compaction (the search dump re-billed 5×), and the
   cost cap checked *after* the spend (hence $6.00 on a $5.00 cap).
4. **Thread-per-request severed the room.** The isolation fix (aria-threads)
   filtered context to the thread's own turns — a brand-new thread saw
   *nothing*, so "that" (the fingerprint pasted 11 seconds earlier in the
   same channel) and "the plan" were unresolvable by construction.
5. **The correctness spec mandated the grind.** "'I couldn't do it'
   responses are FAILED regardless of how polite" judged the clarifying
   question on the bare fingerprint a 0.0 — so the cheap correct move on an
   unresolvable referent was spec-forbidden. Ask → fail; grind → cap → fail.
   Unwinnable.

Proof that prompting cannot fix this class: Aria's own in-thread postmortem
said "A `grep -r honeycomb` would have answered it in one shell call for a
few cents… One grep, done" — and the very next loop spent $5.20 re-running
the archaeology, because the blindness is structural, not attitudinal.

## Symptom → primitive map

| Symptom (observed) | Falls out of the missing ground |
|---|---|
| 26 commands for a one-grep question | no project map in context → blind filesystem sweep |
| $11.70 total; $6.00 on a $5.00 cap | full-price re-billing + post-hoc cap check |
| Answer found in run 1, returned in run 3 | findings discarded at the wall; no carryover |
| "the plan" → 11 search iterations → $5.26 → nothing | no `active_plan` binding anywhere |
| "dont do anything w that" → "that" unresolvable | new thread = zero room context |
| Clarifying question judged 0.0 | spec forbade the cheap correct move |

## The fix — one primitive (ground), few moving parts

> **Give every loop the working set up front; persist what it learns; price
> iterations honestly; let it ask when ground genuinely has no answer.**

| Moving part | File | Moots |
|---|---|---|
| **Ground table** — durable referent → artifact bindings (`active_plan`, `active_project`, `last_artifact`), written at the seams that already know the artifact (`plan_with_claude`, `build_with_cursor`, `cursor_spawn`, the `set_ground` tool), rendered into every loop's context | `src/db.py`, `src/tools.py` | "the plan", "that project" |
| **Projects map in context** — `projects/registry.md` completed (lv3 et al.) and rendered as `projects:` lines, stale paths marked `[MISSING ON DISK]` | `projects/registry.md`, `tools._build_context` | the $3 path hunt |
| **Findings ledger** — every loop exit distills its tool trace (mechanical, no model call) into `loop_findings` keyed by thread; the next run injects it ("do NOT re-run discovery") | `src/db.py`, `tools._do_with_claude_loop` | paying twice; the withheld answer |
| **Prompt caching** — cache breakpoints on system + tool catalog + a moving message breakpoint; cache streams billed honestly (`_estimate_cost` 4-stream) | `tools` | the $0.50/iter static re-bill |
| **Compaction** — tool results older than the last 2 carriers clipped to head | `tools._compact_old_tool_results` | the $0.49→$1.17 snowball |
| **Pre-spend cap** — project next-iteration cost, stop *before* crossing; spend-stops hand over the findings digest | `tools` | $6.00 on a $5 cap; empty-handed pauses |
| **Room continuity** — a new thread inherits the same channel's recent user/aria exchange (`parent_channel` on `Turn`); thread internals and the cursor-watch firehose still never bleed | `src/conversation.py`, `src/bot.py` | "that" / fingerprint class |
| **Discovery backstop** — all-discovery spend ≥ $1.50 stops with the one question + digest; path-misses from discovery tools are PROGRESS, not walls | `tools`, `src/outcomes.py` | blind grinds that ground can't prevent |
| **Spec alignment** — bounded-discovery clarification and explicit no-op instructions judge CORRECT | `specs/correctness/agent.md` | the unwinnable bind |

**Legacy eliminated:** the dormant UCS copy of the agent loop
(`ucs.IntelligenceLoop.execute_agent` + `tools._do_with_claude_ucs`) is
deleted. It had silently drifted (no dollar cap, no local-tool table) and
every reliability fix had to be wired twice. The system has exactly ONE
agent loop: `tools._do_with_claude_loop`.

## What the honeycomb question costs now

Path resolved from `projects:` in iteration 0 (no discovery), ~3 grounded
tool calls, cached prefix from iteration 2 — **≈ $0.60–0.90, no wall**. If a
referent genuinely isn't in ground: ≤ $1.50 of bounded discovery, then one
specific question carrying everything learned.

## Verification

`tests/test_ground_primitive.py` (22) — ground/findings round-trips, context
rendering with `[MISSING ON DISK]`, cache-breakpoint movement, compaction
idempotence, 4-stream billing, discovery-miss classification, the
all-discovery backstop (stops at 3 steps, names `set_ground`), the disarm
case (one grounded call → completes), ledger save-on-stop and
inject-on-resume, and room continuity (inherits same-channel exchange;
other channels and cursor-watch stay out).

Updated: `test_stuck_loop_governor.py` (pre-spend stops at 2 calls — never
one past the cap — and spend-stops carry findings), `test_thread_per_request.py`
(single-loop rename). Full suite: **124 tests, green**; `tests/smoke.py` and
`tests/deep_integration.py` pass (deep run live-proved the `active_plan`
ground writer).

Going live requires a bot restart onto this code (editable install; the
running process predates the change).
