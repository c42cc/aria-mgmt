# Product Output Correctness — Universal Approach

## The Problem

Our systems log what happened but never ask "was it correct?" Failures reach users without the system knowing. This closes that loop for every product.

## The Architecture

Every product implements exactly four layers:

```
EMIT  →  SPEC  →  JUDGE  →  SURFACE
```

### 1. EMIT — Session Record

Every user-facing session produces one retrievable artifact containing:

- **Inputs:** everything the system was given (request, config, plan, context)
- **Outputs:** everything the user experienced (rendered result, final state, generated content)

Completeness test: could an engineer who wasn't there judge whether the output was correct from this record alone? If no, the record is incomplete. Format is team's choice. Most teams already log enough — the work is packaging, not new instrumentation.

### 2. SPEC — Correctness Definition

One text file per product surface, stored in `/specs/correctness/`, version-controlled and reviewed like code. This file declares — in plain language, under 500 words — what properties the output must have given the inputs. Specs must be declarative (properties, not procedures) and objectively verifiable (no taste, no subjective quality). This file is the judge's prompt.

Example: *"Given a lesson plan with N beats, correct means: all beats fired in order, no entities off-screen, no tool operations rejected, final canvas state matches the plan's intended visual outcome."*

### 3. JUDGE — Shared Evaluation Harness

One org-wide harness, owned by one team, used by all products:

```
evaluate(spec, record) → verdict
```

Implementation: sends spec + serialized record to Gemini Flash with a fixed system prompt instructing it to judge whether the outputs satisfy the spec given the inputs. For large records, summarize to evaluation-relevant facts before judging.

Returns a fixed schema every time, for every product:

```json
{ "verdict": "correct | degraded | failed", "score": 0.0-1.0, "reasons": ["specific violation descriptions"] }
```

Product-specific knowledge lives entirely in the spec and the record — never in the harness. The harness is product-agnostic.

### 4. SURFACE — Verdict Store

Every verdict is appended to a queryable store. Start with append-only NDJSON queried via DuckDB. Do not over-engineer this.

Fixed schema: `product | session_id | timestamp | verdict | score | reasons`

This table answers: what % of sessions are correct, by product, over time, and why failures happen.

## Where It Runs

| Context | Mode | Gate |
|---|---|---|
| CI | Blocking | `verdict != "failed"` required to merge |
| Production | Async, post-hoc | Verdicts accumulate; monitor the correctness rate |
| User testing | Every session judged | Primary go/no-go signal |

## Each Team's Deliverables

1. **Session record** — verify your system produces a complete one. Package it.
2. **Correctness spec** — write one file defining correct for your product surface.
3. **Harness integration** — call `evaluate(spec, record)` at session end and in CI.
4. **Verdict emission** — write every verdict to the shared store.

## Scope Boundaries

This system determines *whether* output is correct. It does not fix root causes, intervene during sessions, or evaluate subjective quality. Those are separate efforts. They all depend on this foundation existing first.
