Use when: a change is done and you need proof before declaring it complete.

Verify on live_visuals_4_CC per the Verification Doctrine in `CLAUDE.md`.

1. Run the Stage-1 gate: `bash scripts/quality_gate.sh`. Report GREEN/RED + the pytest
   pass/skip counts and any failing lint.
2. For any visible change, drive the rendered output: boot the server, load it via the
   chrome-devtools browser MCP, take a screenshot, and confirm by inspection that what
   renders matches intent.

State exactly what you observed. If anything was blocked, say "I did NOT verify X
because [reason]; to verify, run [command]." Never declare done on unit-test green
alone, and never on a successful deploy alone.
