# Aria v2 — Architecture

A small, voice-first assistant. The whole system is one idea:

> **Claude conducts the conversation. One engine (Claude Code) does the work.
> Capability is data (a loop file). One brain, many transports. Experience and a
> number are part of "done."**

This replaces v1, whose defect was a non-reasoning front door deciding intent
(see `ABSENCES.md`). v1 is preserved at git tag `aria-v1`.

## The three planes

```
  YOU ── text (Phase 0) ── phone call (Phase 1, LiveKit) ──────────────┐
                                                                        │
  CONTACT (transport only): renders Aria's words, owns turn-taking      │
        text REPL  ·  LiveKit (telephony / STT / TTS / barge-in)        │
                                  │ user utterance ⇅ Aria's line        │
  CONTROL (the brain, portable):                                        │
        AriaBrain ── conductor (Claude: intent→loop→ask→judge)          │
                  ── loops/*.yaml (capability as data)                  │
                  ── memory (pre-fill / skip known slots)               │
                  ── dispatcher (go-gate + ground-truth verify)         │
                  ── outcome log + experience telemetry                 │
                                  │ a confirmed, dispatchable instruction│
  EXECUTION (where work happens):                                       │
        mac-claude-code (Claude Code via the Agent SDK subprocess)      ┘
        + MCP fleet / Home Assistant (later phases)
```

## Primitives

- **Loop** (`loops/*.yaml`, `src/loops.py`) — a unit of capability as data: the
  questions to ask (`required_slots`), how filled answers become an instruction
  (`dispatch`), which endpoint runs it, and what "done" means. Adding a
  capability is adding a file; the core never grows.
- **Conductor** (`src/conductor.py`, `prompts/conductor.md`) — Claude as a
  generic interpreter of any loop. One call per turn returns a structured action
  (`phase` + `speak` + `slots`) via forced tool use. It owns the *content*.
- **Brain** (`src/brain.py`) — `AriaBrain`: the conductor-driving loop, the
  mechanical go-gate, dispatch, and outcome logging in ONE place, so every
  transport is thin and the go-gate has a single home.
- **Engine** (`src/engine_claude_code.py`) — Claude Code via the Agent SDK (a
  managed `claude` subprocess). The one body. Billing is an explicit flag.
- **Dispatcher** (`src/dispatcher.py`) — fills the loop's `dispatch` template +
  the doctrine, runs the engine, and verifies "done" against **ground truth**
  (git diff + an independent test run), never the engine's narration.
- **Memory** (`src/memory.py`) — durable facts that pre-fill loop slots and skip
  settled questions. A UX lever, not storage.
- **Outcome log + telemetry** (`src/outcome_log.py`, `src/telemetry.py`) — one
  append-only row per request (did it deliver) + per-turn latency and full
  conversation traces (so we can debug *feel*).

## Transports

- **Text** (`src/bot.py`, `src/frontends.py`) — the Phase 0 REPL. Built to voice
  rules (one question per turn, short, no markdown) so voice is a swap.
- **Voice** (`src/voice.py`) — LiveKit Agents in front of the same brain.
  `ConductorLLM` is a LiveKit `llm.LLM` whose completion is one `AriaBrain` turn.
  A confirmed build runs in the background so a long run never freezes the call.

## Verified-done (review 1.2)

For code: tests pass + a diff exists + it builds + an honest outcome-log row.
One log. No build-hash receipts, no `live_meter`, no capture+screenshot
agreement as a code gate — that apparatus was v1 scar tissue and is gone. The
capture+Gemini visual check returns only at Phase 4 for genuine physical state.
Every phase carries a number (`docs/aria-v2/preflight.md` §3.7) and does not
advance until met.

## Doctrine reaches the engine (review 3.6)

`prompts/_principles.md` is the one home of the engineering doctrine, injected
via `{{include:_principles}}` (`src/prompts.py`) into the conductor persona AND
into the engine instruction the dispatcher builds. `.cursor/rules/*.mdc` govern
the build-time IDE agent only; they are inert to Aria's runtime engine.

## Repo map

| Concept | Where |
|---|---|
| The text loop (legible) | `src/bot.py` |
| The voice transport | `src/voice.py` |
| The brain (go-gate, dispatch) | `src/brain.py` |
| Conductor (Claude owns content) | `src/conductor.py` + `prompts/conductor.md` |
| Loops (capability as data) | `loops/*.yaml` + `src/loops.py` |
| Engine (Claude Code) | `src/engine_claude_code.py` |
| Dispatcher (ground-truth verify) | `src/dispatcher.py` |
| Memory / outcome log / telemetry | `src/memory.py` · `src/outcome_log.py` · `src/telemetry.py` |
| Config (one home) | `src/config.py` + `.env` |
| Boot preflight | `src/preflight.py` |
| Section-3 decisions + Phase-1 setup | `docs/aria-v2/` |
| What was removed, and why | `ABSENCES.md` |

## Phases

0 text spine (done) · 1 the phone call (bridge done; audio/telephony needs
LiveKit provisioning) · 2 loop library + measurement · 3 mesh relocation +
trust boundary · 4 the house · 5 proactivity · 6 cloud + fan-out.
