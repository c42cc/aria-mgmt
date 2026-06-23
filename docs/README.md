# docs/

Supporting documentation. The **canonical** docs live in the repo root:
[`README.md`](../README.md), [`ARCHITECTURE.md`](../ARCHITECTURE.md),
[`VISION_ARIA.md`](../VISION_ARIA.md), [`VISION_CONSTRUCTOR.md`](../VISION_CONSTRUCTOR.md),
and [`wiring.md`](../wiring.md). This folder holds everything that supports them.

| Path | What it is |
|---|---|
| `product-correctness-approach.md` | The EMIT → SPEC → JUDGE → SURFACE methodology behind `specs/correctness/` + `src/judge.py`. |
| `universal_constructor.html` | The Universal Constructor north-star: a standalone explainer + live trace of the prompt engine Aria wields (code in `src/constructor/`, vision in [`VISION_CONSTRUCTOR.md`](../VISION_CONSTRUCTOR.md)). |
| `forensics/` | Deep-dive analyses of specific subsystems (threads, ground, progress governor). |
| `archive/` | Historical planning briefs, kept for provenance. Superseded by the root docs; **not maintained**. |
