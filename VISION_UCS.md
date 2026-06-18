# UCS — Universal Capability System

## Vision

A self-improving prompt orchestration engine that manages, injects, executes, and evaluates prompts across heterogeneous model backends. Designed to be portable — runs locally, ships to friends, works everywhere.

---

## Core Primitives

The system reduces to four irreducible components:

1. **Prompt Library** — A curated, version-controlled store of atomic prompts, indexed by functional use case or capability.
2. **Injection Engine** — The mechanism that assembles, injects, and chains prompts into model calls to perform and verify work.
3. **Intelligence Loop** — A tight 2–4 step execution cycle that hot-swaps models per step based on task requirements.
4. **Evaluation Layer** — A closed-loop feedback system that scores prompt output against expected baselines and self-corrects.

Everything else in the system is a composition of these four.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    UCS Runtime                       │
│                                                      │
│  ┌──────────┐   ┌──────────────┐   ┌──────────────┐ │
│  │  Prompt   │──▶│  Injection   │──▶│ Intelligence │ │
│  │  Library  │   │  Engine      │   │ Loop (2–4)   │ │
│  └──────────┘   └──────────────┘   └──────┬───────┘ │
│                                           │         │
│                    ┌──────────────────────┐│         │
│                    │   Model Router       ││         │
│                    │ ┌───────┬──────┬───┐ ││         │
│                    │ │Claude │Local │Gem│ ││         │
│                    │ │       │(Olla-│ini│ ◀┘         │
│                    │ │       │ma)   │   │ │          │
│                    │ └───────┴──────┴───┘ │          │
│                    └──────────────────────┘          │
│                           │                          │
│                    ┌──────▼───────┐                  │
│                    │  Eval Layer  │──▶ delta → lib   │
│                    └──────────────┘                  │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │            MCP / Tool Integration             │   │
│  │  (filesystem, git, browser, APIs, custom)     │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

---

## MVP (v0.1)

### Prompt Library

- **Curated, not generated.** 1–2 hand-selected prompts per functional use case or capability. Quality over quantity.
- **Flat file storage.** Prompts live as markdown or YAML files in a `/prompts` directory, organized by capability domain (e.g., `code-review/`, `summarization/`, `data-extraction/`).
- **Metadata per prompt:** id, version, target model affinity (which model it was tuned for), use case tag, expected output schema.
- **Selection logic:** When a task arrives, the system matches it to a use case, pulls the best prompt for the routed model, and hands it to the injection engine.

### Injection Engine

- Assembles the final payload: system prompt + context window + user input + verification suffix.
- Supports **prompt chaining** — output of step N becomes input to step N+1 within the intelligence loop.
- Injects **verification prompts** as a second pass: after work is performed, a separate prompt (potentially on a different model) checks the output against defined constraints.
- Handles **template interpolation** — prompts contain `{{variable}}` slots filled at runtime from task context.

### Intelligence Loop

- Fixed 2–4 step cycles. No unbounded recursion. Every loop has a defined entry, execution, verification, and exit.
- **Model hot-swapping per step.** Step 1 might use Claude for reasoning, step 2 a local model for fast structured extraction, step 3 Gemini for a second opinion on ambiguous output.
- **Parallel execution.** Independent loops run concurrently. A batch of 10 tasks spawns 10 loops, each routing to the optimal model per step based on the router's cost/latency/quality heuristics.
- **Circuit breaker.** If verification fails twice consecutively, the loop halts and escalates rather than retrying infinitely.

### Model Router

- Configuration-driven. A `models.yaml` maps model IDs to endpoints, API keys, rate limits, and capability tags.
- **Affinity matching.** Each prompt in the library declares which models it performs best on. The router respects this unless overridden by latency or availability constraints.
- **Fallback chains.** If the primary model is down or rate-limited, the router cascades to the next best option automatically.

### MCP & Tool Integration

- First-class MCP client. The system can call any MCP server (filesystem, git, browser, Slack, databases, custom tools) as part of a loop step.
- Tools are declared per-capability in the prompt library metadata — a code-review prompt might declare dependencies on `git` and `filesystem` MCP servers.
- Tool results feed back into the injection engine as context for subsequent steps.

---

## GOLD (v1.0)

### Self-Evaluating Prompt Engine

The eval layer closes the loop between execution and prompt improvement:

1. **Baseline definition.** For each use case, define 3–5 reference input/output pairs that represent "perfect alignment" with end-user expectations.
2. **Multi-environment testing.** Run the same prompt against multiple model backends and capture outputs.
3. **Delta scoring.** Compute the divergence between actual output and the reference baseline using a composite metric:
   - Structural alignment (does the output match the expected schema/format?)
   - Semantic fidelity (does it preserve meaning? scored by a judge model)
   - Constraint satisfaction (did it respect all hard requirements?)
4. **Prompt ranking.** When multiple prompt variants exist for the same use case, rank them by aggregate delta score across environments.
5. **Feedback injection.** The winning prompt variant gets promoted in the library. Losing variants are retained with their scores for historical analysis.
6. **Drift detection.** Periodic re-evaluation catches model-side changes (API updates, weight changes) that degrade previously high-performing prompts.

### Evaluation Workflow

```
Prompt A (v2.3)  ──▶  Claude    ──▶  Output A1  ──┐
                 ──▶  Local     ──▶  Output A2  ──┤
                 ──▶  Gemini    ──▶  Output A3  ──┤
                                                   ▼
Prompt B (v1.1)  ──▶  Claude    ──▶  Output B1  ──┤  Delta
                 ──▶  Local     ──▶  Output B2  ──┤  Scorer
                 ──▶  Gemini    ──▶  Output B3  ──┤
                                                   ▼
                                              ┌─────────┐
Reference Set ─────────────────────────────▶  │ Compare │
(expected outputs)                            └────┬────┘
                                                   │
                                    ┌──────────────▼──────────────┐
                                    │ Promote best │ Log losers   │
                                    │ to library   │ with scores  │
                                    └─────────────────────────────┘
```

---

## Portability & Distribution

The system is designed to be packaged and handed to someone who can run it locally:

- **Single config entry point.** One `config.yaml` where the user drops in their API keys, model endpoints, and preferred defaults. Everything else works out of the box.
- **No cloud dependency for core function.** The prompt library, injection engine, and loop orchestrator run entirely local. Cloud models are called as remote endpoints, same as local ones — the router abstracts the difference.
- **Docker-optional.** Runs natively with Python/Node or inside a container. `docker compose up` for zero-config setup; direct execution for people who prefer it.
- **Startup wizard.** On first run, the system detects available models (local Ollama instances, API keys present in env) and auto-configures the router. No manual YAML editing required unless you want to customize.

---

## Additional Capabilities

### Prompt Version Control & Rollback

Every prompt edit creates a new version. The library maintains a full history per prompt so that when the eval layer detects a regression, it can automatically roll back to the last known-good version without human intervention.

### Context Budget Manager

Each model has a different context window. The injection engine tracks token usage per assembled payload and automatically compresses, truncates, or splits context when approaching limits — rather than failing silently or letting the model hallucinate from a clipped input.

### Observability & Cost Tracking

Every loop execution logs: model used per step, token counts in/out, latency, cost estimate, eval scores, and pass/fail status. This surfaces as a local dashboard (simple HTML) so the operator can see what's expensive, what's slow, and what's failing — without needing external monitoring infrastructure.

### Prompt Composability

Atomic prompts can be composed into chains declaratively. A `pipeline.yaml` defines sequences like: "first run `extract-entities`, then run `classify-sentiment` on each entity, then run `summarize-findings` on the batch." The injection engine resolves the DAG and parallelizes where the dependency graph allows.

---

## Principles

- **Small loops, not agents.** Bounded execution with explicit exit conditions. No open-ended "figure it out" behavior.
- **Curated over generated.** A small number of excellent prompts outperforms a large number of mediocre ones.
- **Models are interchangeable.** The system is model-agnostic by design. No prompt should be so coupled to one model that it can't run (with some delta) on another.
- **Portability is a feature.** If it doesn't run on someone else's machine in under 5 minutes, it's not done.
- **Measure everything.** If a prompt isn't being evaluated, it's rotting. The eval layer isn't optional — it's what makes the system a system instead of a script collection.

---

## Implementation Status

UCS was integrated into Aria using a phased approach: grow each piece
alongside the existing system, prove it independently, then let the
unification emerge naturally.

| Primitive | Status | Where |
|---|---|---|
| Prompt Library (version control) | **Implemented** | `src/prompts.py` — version archival in `prompt_versions` table with origin tracking, rollback by voice |
| Prompt Library (metadata/affinity) | Deferred | Not yet needed — Phase 1 data doesn't show model-specific prompt tuning demand |
| Injection Engine (context budget) | **Folded in** | Context-budget truncation now lives in the single agent loop; `loop_executions` still records `context_truncated` / `turns_dropped`. |
| Intelligence Loop | **Removed** | The flag-gated `src/ucs.py` `IntelligenceLoop` (`UCS_ENABLED`) was a dormant duplicate of the agent loop and was deleted; there is one loop, in `src/tools.py`. |
| Model Router | **Removed** | `src/ucs.py` `ModelRouter` went with the flag; `models.yaml` is still read for model config (`src/config.py`, `src/tools.py`). |
| Evaluation Layer | **Implemented** (offline CLI) | `src/eval.py` — approval-rate scoring from execution logs |
| Model Registry | **Implemented** | `models.yaml` — single source of truth for models, costs, capabilities |
| Execution Logging | **Implemented** | `loop_executions` table — every reasoning call logged with model/tokens/latency/cost |
| Prompt Composability | Deferred | Not yet needed |
| Parallel Loop Execution | Deferred | Serial is sufficient |
| Local Dashboard | Deferred | SQL queries suffice |

**Governance:** User voice edits to prompts always win. The eval layer
advises; it does not override. See `ARCHITECTURE.md` Fundamental 13.
