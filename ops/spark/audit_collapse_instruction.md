You are Claude Code running headless and autonomous on a DGX Spark node, inside a
synced checkout of **live_visuals_4**, on a fresh `collapse/<date>` branch that
has already been created for you. Run to completion without asking questions;
there is no human at this node. Your control-plane (`CLAUDE.md`, `.claude/`) is
already loaded — re-read `CLAUDE.md` now for the law, billing, and git rules.

# Mission
Re-execute the repeatable forensic audit, then **perform** the collapses it
names — diagnosis AND surgery — leaving the tree GREEN on this branch.

# Phase 1 — Diagnose (refresh the ledger)
1. Read the brief verbatim: `_MASTER_REPEATABLE_AUDIT_FORENSIC_AUDIT_COLLAPSE_TO_PRIMITIVES.md`.
   Follow it exactly. Measure only against the repo's own law
   (`_INVIOLABLES.md`, `_ARCHITECTURE.md`, `_THE_SPINE.md`, the
   `_*_PRIMITIVE.md` family) — import no outside criteria.
2. Execute the audit against the **current HEAD** (the tree may have moved since
   the last ledger; re-derive, do not trust the stale `path:line`s).
3. Overwrite `TODO_GO_FORWARD_FORENSIC_AUDIT_COLLAPSE_LEDGER.md` with the
   refreshed ledger (same structure: §0 invariants quoted back, the inventories,
   the collapse ledger sorted by severity, and the §3 collapse sequence).
4. `git add TODO_GO_FORWARD_FORENSIC_AUDIT_COLLAPSE_LEDGER.md && git commit`
   with message `audit(refresh): re-execute forensic ledger @ <short-head>`.

# Phase 2 — Collapse (go beyond the brief's diagnosis-only stance, here, on this branch)
Work the refreshed ledger's **§3 collapse sequence in order**, one wave at a time.
Every finding must terminate in Collapse / Derive / Subtract / Split — **never** a
parity test, a reconciler, a retry, a fallback default, or a tuned threshold (the
two disqualifiers in §0 of the brief). Every fix is subtraction of a second home /
a default, not an added guard.

For EACH wave:
1. Perform every collapse in that wave, citing the law row in the commit body.
2. Run the gate: `bash scripts/quality_gate.sh` (G5 every `scripts/lint_*.sh`
   passes + G6 `pytest tests/`).
3. If GREEN: `git add -A && git commit -m "collapse(wave-N): <findings> — gate green"`.
   If RED: do NOT commit broken work and do NOT paper over it with a checker or a
   default. Fix the collapse so the gate is honestly green. If a wave cannot be
   made green after a genuine attempt, STOP: leave the working tree as-is, write
   what blocked you and your evidence to the end of the ledger under a
   `## Spark run — halted` heading, commit that note, and end the run. A partial,
   honest, reviewable result beats a green-looking lie.

# Hard rules (from CLAUDE.md — do not violate)
- NEVER `git push`; NEVER commit to `main`; stay on the `collapse/<date>` branch.
  The Mac pulls your branch back and pushes it.
- NEVER `source .env` and NEVER set `ANTHROPIC_API_KEY` (would shadow the Max
  subscription). `git reset --hard`, `git clean -fdx`, `rm -rf`, `sudo` are denied.
- Keep the control-plane files (`.claude/`, `.mcp.json`, `CLAUDE.md`) out of every
  commit (they are already in `.git/info/exclude`).

# Finish
End with a concise final message: which waves landed, the gate verdict, the
commit shas on this branch, and any finding deferred or halted. That message and
your commits are the deliverable; Aria fetches the branch + the refreshed ledger
back to the Mac.
