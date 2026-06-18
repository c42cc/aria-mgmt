Use when: a plan has been reviewed and approved — execute it on live_visuals_4_CC.

Execute the approved plan. Hold `CLAUDE.md`'s inviolables: no retries, no fallbacks,
no hard-coded values (one home in configs/parameters.yaml), one fact one home,
structure not watchers. Make the smallest complete change across all affected layers;
edit the source-of-truth doc first when the design changes, then the code.

When done, run the Stage-1 gate yourself — `bash scripts/quality_gate.sh` — and report
GREEN/RED with the pytest pass/skip counts. For any UI/visual change, also verify the
rendered output (boot the server, drive it via the chrome-devtools browser MCP,
screenshot, inspect): the only proof is the rendered output the user sees. If a gate
fails, STOP and report what you observed at the failing point — a failed gate re-scopes
the design; never heal past it.

Approved plan:
{{plan}}
