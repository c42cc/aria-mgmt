# Aria v2 — Wiring

How the pieces connect. v2 has almost no IPC by design — the sidecar sprawl of
v1 is gone (`ABSENCES.md`).

## Processes

- **One Python process** holds the brain + the active transport. Text mode is
  `python -m src.bot`; voice mode is `python -m src.voice` (a LiveKit worker).
- **The engine spawns a `claude` subprocess per build** (the Claude Agent SDK
  manages it; no long-running sidecar). It streams events back; the dispatcher
  reads them and then checks ground truth.
- **Outbound HTTPS to Anthropic** for the conductor (the brain). Phase 1 adds
  LiveKit (audio/telephony) and STT/TTS providers.

## A turn, end to end

```
user utterance
  → transport (text REPL / LiveKit STT)         # renders + turn-taking only
  → AriaBrain.user_turn(text)
       → conductor.decide(transcript, loops)     # Anthropic API: Claude owns content
       → returns {phase, speak, loop_id, slots}
  → transport speaks `speak`                      # text print / LiveKit TTS
  → if phase==DISPATCH and the go-gate holds:
       → dispatcher.run(loop, slots)
            → build instruction = loop.dispatch + {{include:_principles}}
            → engine_claude_code.run(repo, instruction)   # claude subprocess
            → verify GROUND TRUTH: git diff + an independent test run
       → outcome_log.record(...)                  # the one measurement
       → conductor REPORT turn → transport speaks the honest result
```

Text **blocks** on the build and reports inline; voice speaks a filler and runs
the build in the **background**, reporting via the loop's channel — so a
multi-second engine run never freezes the call.

## State (three flat files under `data/`, gitignored)

- `data/outcomes.jsonl` — one row per request (did it deliver).
- `data/traces/<session>.json` — per-turn latency + full conversation trace.
- `data/memory.json` — durable facts that pre-fill slots.

## Config & secrets

`src/config.py` loads `.env` once into a frozen dataclass; nothing else reads the
environment. Secret fields are `repr=False` (no key leaks into a log). In the
`aria-v2` worktree, `.env` is a symlink to the main checkout — one copy, never
duplicated. Model ids are pinned to verified-live values; `src/preflight.py`
asserts each resolves before the user can hit it.
