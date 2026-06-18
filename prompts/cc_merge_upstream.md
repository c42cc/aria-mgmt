Use when: pulling in changes from the in-development upstream live_visuals_4.

Merge upstream `live_visuals_4` deltas into live_visuals_4_CC per `MIGRATION_PROVENANCE.md`.

1. Compute the upstream delta: in ../live_visuals_4, `git diff <recorded-fork-sha> HEAD`
   (the fork SHA is recorded in MIGRATION_PROVENANCE.md).
2. Apply it here, resolving the few conflicts that land on citation lines or files the
   migration also touched (the .cursor -> CLAUDE.md / .claude rename, the
   cursor-ide-browser -> chrome-devtools MCP remap).
3. If upstream introduced NEW `.cursorrules` / `*.mdc` citations or `cursor-ide-browser`
   tool names, re-apply the documented mechanical transforms, then re-run the B.6 sweep
   (zero stale tokens outside intentional notes).
4. Run the Stage-1 gate (`bash scripts/quality_gate.sh`) GREEN before declaring the
   merge done. Report what changed and anything you could not cleanly reconcile.
