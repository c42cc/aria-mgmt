# Aria v2 — Section-3 pre-flight (answered on paper before Phase 0)

The review's Section 3 is a checklist that must be answered before code, not
discovered at runtime in front of the user. Each item below is a decision +
what was verified on 2026-06-21.

## 3.1 Billing + cost model + model tiering
- **Verified:** the June-15 "separate Agent SDK credit pool" change was
  **paused/reversed**. Today `claude -p` / Agent SDK usage **still draws from
  the Claude subscription**. (Sources: Anthropic support notice; The New Stack;
  Computing.co.uk — all June 15-16, 2026.)
- **Decision:** billing is an explicit config flag `ARIA_CLAUDE_CODE_BILLING`
  (`subscription` | `api`), default `subscription` (cheapest, works today).
  If subscription auth is absent the engine fails **loudly** with the one-line
  fix (`claude login`, or flip to `api`) — never a silent fallback.
- **Tiering:** the conductor (genuine reasoning) is the only place Opus is
  spent; routine extraction will move to a verified cheap/fast model
  (`ARIA_FAST_MODEL`) once that loop exists. An always-on assistant firing Opus
  every turn is the cost risk; the fast/smart split is the mitigation.

## 3.2 Model IDs (no stale/retired pins)
- **Verified live:** Opus = `claude-opus-4-8` (current; $5/$25 per MTok; 1M ctx;
  adaptive-thinking only — manual `thinking:{type:enabled}` is a 400). Gemini
  Live = `gemini-3.1-flash-live-preview` (current, recommended; **synchronous
  function-calling only** — a tool call blocks the session until it returns).
- **Retired June 15 (hard error if pinned):** Sonnet 4 / Opus 4 `…-20250514`.
- **`ARIA_FAST_MODEL` (`claude-haiku-4-5`) is UNVERIFIED** — not wired in
  Phase 0; verify before first use.
- **Enforcement:** `src/preflight.py` pings each configured model id with a
  1-token request at boot; a dead id fails ready, never surfaces to the user.

## 3.3 The SDK is a managed subprocess (not in-process)
- The Claude Agent SDK runs the `claude` binary as a subprocess and streams
  events. `src/engine_claude_code.py` wraps that real model. "No sidecar" means
  no separate long-running service (unlike v1's Cursor Node wrapper); the SDK
  owns the subprocess per run.

## 3.4 Trust boundary (must be real before Phase 3)
- Phase 0/1 are **co-located** on the Mac: the engine and core share the host,
  so there is no network trust boundary yet — and the engine runs only
  instructions built from the user's own words (see 3.8).
- Before the core travels (Phase 3): mutual auth core↔endpoint; least-privilege
  per-endpoint capability scoping (the Mac runner is NOT an open exec port — it
  exposes named loops, not arbitrary shell); explicit blast-radius statement for
  a compromised core. This is a gating deliverable of Phase 3, designed before
  any remote endpoint exists.

## 3.5 Secrets strategy (before the core travels)
- Phase 0: `.env` in the worktree is a **symlink** to the main checkout's `.env`
  — one copy on one host, never duplicated.
- Before Phase 3: keys scoped per host, present only where used (a manager or
  per-endpoint env), so a traveling core does not carry the whole keyring.

## 3.6 Doctrine reaches the ENGINE, not just the IDE agent
- `.cursor/rules/*.mdc` govern the build-time Cursor agent only; they are inert
  to Claude Code at runtime. So Aria's runtime doctrine travels via Claude
  Code's real primitives: the dispatch instruction is built from an
  `{{include:_principles}}` persona, and the dispatcher writes a `CLAUDE.md`
  (doctrine) into the workspace before the run. Phase 0 verifies a real run can
  echo a doctrine marker, proving it reached the engine.

## 3.7 A number, not "reliable"
- **Phase 0 bar:** of 10 real feature-build requests, **≥ 9 reach a correct,
  honestly-logged outcome** (engine produced the intended diff AND its tests
  pass AND it builds), AND the conductor met the experience bar on each:
  one question per turn, ≤ ~2 short sentences per turn, a wrong-turn correction
  absorbed without derailing, and no fabricated success. A phase does not
  advance until its number is met; a miss is recorded, not waved through.

## 3.8 Untrusted content → executor with shell access
- **Phase 0/1 are safe by construction:** every loop instruction is built only
  from the user's own words + the loop template. No loop ingests external
  content yet.
- **Becomes a live gate at Phase 2 (research-brief):** external content (web
  page, email body, HA state) is labeled DATA and quarantined — summarized by a
  bounded reasoner and never concatenated into the executor's instruction as
  trusted text. The executor receives instructions, never raw foreign content.

## 3.9 Text Phase 0 obeys voice rules
- The conductor persona enforces voice shape from day one: one question per
  turn, short utterances, no markdown / no bulleted lists, interruptible,
  read the plan back briefly. So Phase 1 is a transport swap, not a rewrite.
