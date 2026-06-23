# Docs index — the master map

The canonical documents for this project, with the recommended reading order.
Start at the top; each links onward. Code is the ground truth; these explain it.

## Start here

1. [`README.md`](README.md) — what this is and how to run it.
2. [`ARCHITECTURE.md`](ARCHITECTURE.md) — **the system map**: primitives, layers,
   capabilities, and the Repo Map (where every concept lives). Read this first
   when you need to find where something is.
3. [`wiring.md`](wiring.md) — how the pieces connect: process topology, IPC,
   boot/voice/tool lifecycles, failure modes.

## Vision

- [`VISION_ARIA.md`](VISION_ARIA.md) — what Aria is and where she goes.
- [`VISION_CONSTRUCTOR.md`](VISION_CONSTRUCTOR.md) — the Universal Constructor
  (the editable-behavior subsystem Aria wields).

## Capabilities & subsystems

- [`docs/local-spark-agent.md`](docs/local-spark-agent.md) — **Local Spark
  Agent**: a local-brained chat window (open-source model on the DGX Spark,
  served behind the Anthropic Messages API, driving the unchanged agent loop +
  MCP fleet from a browser on the LAN/Tailscale).
- [`ops/spark/NODES.md`](ops/spark/NODES.md) — the two GB10 DGX Spark nodes: how
  we reach/verify them (Sections A/B) and serving (Section C).
- [`docs/product-correctness-approach.md`](docs/product-correctness-approach.md) —
  the EMIT → SPEC → JUDGE → SURFACE methodology behind `specs/correctness/` +
  `src/judge.py`.
- [`docs/forensics/`](docs/forensics/) — deep dives on specific subsystems
  (threads, ground, progress governor).

## Engineering discipline (enforced)

- [`prompts/_principles.md`](prompts/_principles.md) — the one home for the
  engineering doctrine, `{{include:_principles}}`'d into every reasoning/build
  persona.
- `.cursor/rules/` — the always-applied rules: `one-trunk`, `halt-dont-heal`,
  `done-means-verified-on-this-build`, `finish-the-job`, `ucs-architecture`.
- [`configs/structural_absences.json`](configs/structural_absences.json) — the
  enforce-by-absence ledger (what was collapsed must stay gone). Checked by
  `tools/structural_absence_check.py` in the one gate, [`scripts/gate.sh`](scripts/gate.sh).

## Archived / historical

- [`docs/archive/`](docs/archive/) — superseded planning briefs, kept for
  provenance. Not maintained.
