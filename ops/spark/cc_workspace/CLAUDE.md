# live_visuals_4 — Spark Claude Code workspace

You are operating on a **DGX Spark node** inside a synced checkout of
`live_visuals_4`, driven headlessly by Aria. This file plus `.claude/` and
`.mcp.json` are the same control-plane the project uses on the Mac; they are
overlaid here and are **git-excluded** (never commit them).

## The law lives in this repo — read it, do not import outside criteria
The yardstick is the repo's own law and primitives. Cite the home in every
finding. Start from these, in priority order:
- `_INVIOLABLES.md` (The Law), `_ARCHITECTURE.md` (§10 architecture test),
  `_THE_SPINE.md` (§6 spine test), and the `_*_PRIMITIVE.md` family.
- The repeatable brief: `_MASTER_REPEATABLE_AUDIT_FORENSIC_AUDIT_COLLAPSE_TO_PRIMITIVES.md`.

## Billing — subscription only
Auth is the Max subscription. **Never `source .env`** in any shell, and never
set `ANTHROPIC_API_KEY`: either would shadow the subscription and bill per token.

## Git discipline (the Mac is the boundary)
- Do all work on the branch Aria created (`collapse/<date>`); **never** commit to
  `main`.
- **Never `git push`** — this node has no remote credentials; the Mac pushes
  after pulling your branch back. Force-push, `git reset --hard`, and
  `git clean -fdx` are denied.
- Commit per logical step with clear messages so each wave is reviewable.

## Green is the gate
"Done" means `bash scripts/quality_gate.sh` exits **GREEN** (G5 every
`scripts/lint_*.sh` passes + G6 `pytest tests/`). Run it after each wave; if it
goes RED, stop and leave the evidence — do not paper over it.
