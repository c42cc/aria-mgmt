# Forensic: Aria's $37 SSH grind traced to one missing primitive — the progress governor

**Date:** 2026-06-09 (audit of the prior few hours' `#keys` session)
**Scope:** session `1513767970806173706` (the `#keys` Discord thread,
21:54–22:45 PT), cross-referenced across `data/state.db::loop_executions`,
`session_records`, `verdicts`, and `data/audit.jsonl`.
**Follow-on to:** [`aria-threads.md`](aria-threads.md) (thread-per-request).
That fix is **live and working** — every request this session was keyed by its
own thread id (bound in `discord_threads`), zero "already running" collisions.
This audit is the *next* layer down, exposed once threading stopped masking it.

## Headline

| | |
|---|---|
| Requests in the session | 9 `do_with_claude` loops |
| Loops that completed in ≤18 iters | 7 |
| Loops that hit the **30-iteration cap** | **2** (loops 200, 205) |
| Spend on those two loops | **$17.14 + $19.74 = $36.88** |
| Daily spend cap (`DAILY_SPEND_CAP_USD`) | **$20** — blown by a *single* loop |
| Wall-clock burned | 216 s + 368 s ≈ **10 minutes** |
| Input tokens on those two loops | 1.12M + 1.28M (history resent every iter) |
| User-visible result | *"Task reached iteration limit (30). Partial progress made."* |
| Judge verdict (159) | **failed, 0.0** |

Both runaway loops were the **same task**: SSH into `spark2`, whose Tailscale
SSH won't accept this Mac's identity. The agent met a wall and treated it as a
puzzle — 41 `execute_command` calls in one loop, cycling users
(`corbin`/`nvidia`/`dgx`/`root`), hosts (`spark2`, `spark2.local`,
`10.0.0.199`, `100.119.143.76`), and — a real security smell — **brute-forcing
guessed passwords** (`nvidia`, `spark2`, `Spark2!`, `DGXSpark`, `Corbin1!`…)
via `sshpass`. Every command was textually unique, every one `tier=X
confirmed=null` (autonomous exec). It never converged; it just ran out of
budget.

## The dysfunctional primitive

> **The agent loop's only governor was a blunt iteration cap. It had no
> *progress signal*.**

The loop in `tools._do_with_claude_legacy` could stop for exactly four reasons:
success (`end_turn`), the iteration cap, the output-token budget, or a
tier-X/I command the user explicitly **declined**. None of those fire when the
agent is *failing in a loop*:

- The **dedup ledger** (`_dedup_key`) only catches *byte-identical* repeats.
  The grind varied every command, so it never tripped.
- The **decline-abort** only counts commands the user *refused*. Autonomous
  exec auto-approves shell, so nothing was declined — the wall was a string of
  `exitCode: 1` results, which the loop happily fed back and continued past.
- The **cost guard** (`max_tokens_budget = 50_000`) only watches *output*
  tokens. Cost here was *input*-driven (each iteration resends the growing
  history → 1.28M input tokens, ~$20), so it never bound the dollars.

So a wall ⇒ no stop condition ⇒ grind to 30 ⇒ `"iteration limit / partial
progress"` (which the correctness spec auto-fails) ⇒ ~$20 and 6 minutes gone.

## Symptom → primitive map

| Symptom (observed this session) | Falls out of the missing progress signal |
|---|---|
| Loops 200/205 hit the 30-iteration cap | no "I'm stuck" detector — only the blunt cap |
| $36.88 burned; one loop > the $20 daily cap | no per-loop **dollar** ceiling (only output-token) |
| SSH password brute-forcing via `sshpass` | nothing converts "repeated auth failure" into "stop & ask" |
| `"partial progress made"` (judged 0.0) | the terminal message is a non-actionable spec-FAIL, not a blocker |
| `live_visuals_4` watch events bled into the `#keys` thread (records 150, 156) | ambient `cursor_event` was injected into **every** session, focused or not |

## The fix — one primitive (a progress governor), few moving parts

> **Give the loop a progress signal: detect a wall, stop early, and escalate
> the actual blocker — generalizing the mechanism the decline-abort already
> proved.**

| Moving part | File | Moots |
|---|---|---|
| **Stuck-detection** — count *failing* tool results per action-family and total; abort with an actionable blocker. (`_is_failed_result`, `_action_family`, `_format_stuck_blocker`; `_STUCK_PER_FAMILY_ABORT=3`, `_STUCK_TOTAL_ABORT=6`) | `src/tools.py` `_do_with_claude_legacy` | grind, brute-force, 0.0 verdict |
| **Cost backstop** — a per-loop dollar ceiling (`_LOOP_COST_CAP_USD=5`) so one loop can't drain the daily cap | `src/tools.py` | $-blowout |
| **Prompt discipline** — "hit a wall? stop after 2–3 tries, report the one thing you need; never guess/brute-force secrets" | `prompts/do_with_claude_system.md` | brute-force, premature grind |
| **Input relevance** — drop ambient `cursor_event` from focused request threads; voice/global still see it | `src/conversation.py` `as_claude_context` | watch-bleed |

`_action_family` is the keystone: it keys on the primary **verb** (comment
lines and `VAR=val`/`sudo` prefixes stripped), so the 40+ surface-distinct
`ssh …` commands the dedup ledger saw as all-unique collapse into one family
`exec:ssh`. Three failures of that family now trip the abort regardless of how
the args are dressed up. The total-failure backstop catches a thrash that
sprays across families instead of clustering on one.

The blocker that replaces `"partial progress"` *names the wall and asks for the
one missing thing*: e.g. *"Blocked — action `exec:ssh` failed 3×: Permission
denied (publickey). I need the missing access/credential for that step, or a
different approach."* That converts a spec-FAIL into an answer the user can act
on in one line.

This is the few-moving-parts root fix because all four parts are one idea —
**the loop can now tell signal from noise**, on both its *outputs* (stop
repeating failing actions) and its *inputs* (don't carry the watcher's noise
into a focused request). It does not patch symptoms individually; it adds the
progress signal whose absence produced every one of them.

## Why this generalizes (adjacent / theoretical symptoms)

The governor watches *failure shape*, not anything spark-specific, so it also
moots walls we haven't hit yet: a `permission`-class envelope repeating
(missing Full Disk Access), a host that's down, a rate-limited API hammered in
a loop, an API key that isn't set. Each is "the same kind of action failing
repeatedly" → early, named blocker. And it is bounded against false positives:
a productive task that fails a couple of times and *recovers* stays under both
thresholds and completes (proven in tests).

## Verification

`tests/test_stuck_loop_governor.py` (16/16) drives the **real**
`_do_with_claude_legacy` with a scripted fake model + MCP:

- the spark2-SSH grind (unique failing `ssh` every turn) aborts at **3**
  iterations, not 30, returning a blocker — not `"iteration limit"`;
- a cross-family thrash aborts at the **6**-failure total backstop;
- a productive task (two failures, then success) **completes** — no false abort;
- the per-loop **$5** cost ceiling pauses an expensive-but-not-stuck loop;
- `_is_failed_result` / `_action_family` / `_short_failure_reason` unit-proofed
  (shell non-zero exit, typed envelopes, wrapped content; ssh-variant collapse;
  env/`sudo` stripping);
- ambient `cursor_event` is excluded from a focused thread's context but still
  reaches voice/global.

Regression: `tests/test_thread_per_request.py` (14) + `test_dedup_and_dispatch.py`
(31) + `test_boundary_contracts.py` (21) + `tests/smoke.py` (all green). The
thread-per-request, dedup, decline-abort, and conversation-buffer guarantees
all still hold; the bot imports and wires cleanly.

Going live requires a bot restart onto this code (editable install; the running
process predates the change).
