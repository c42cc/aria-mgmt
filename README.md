# Aria v2

A small, voice-first assistant rebuilt the right way. The shape:

> **Claude conducts the conversation. One engine (Claude Code) does the work.
> Capability is data (a loop file). Experience and a number are part of "done."**

- **Conductor (`src/conductor.py`).** Claude owns the conversation's content —
  it understands the intent, picks the loop, decides what to ask, and judges the
  plan. One Claude call per turn, returned as a structured action (forced tool
  use). A fast voice layer (Phase 1) only renders what Claude says; it never
  decides. This is the fix for the defect that sank v1.
- **Loops (`loops/*.yaml`).** Capability as data: the questions to ask, how the
  answers become an instruction, which endpoint runs it, what "done" means.
  Adding a capability is adding a file.
- **Engine (`src/engine_claude_code.py`).** Claude Code via the Agent SDK — a
  managed `claude` subprocess. The one body. No Cursor.
- **Dispatcher (`src/dispatcher.py`).** Fills the loop template + the doctrine,
  runs the engine, and verifies "done" against **ground truth** (git diff + the
  test exit code), never the engine's narration.
- **Outcome log + telemetry (`src/outcome_log.py`, `src/telemetry.py`).** One
  append-only log of did-it-deliver, plus per-turn timing and full conversation
  traces so we can debug *feel*, not just correctness.

## Run

```bash
python -m venv .venv && .venv/bin/pip install -e . pytest
make gate     # static: the unit suite
make run      # text mode — type a request; /quit to exit
```

Keys live in `.env` (here, a symlink to the main checkout — secrets aren't
duplicated). Phase-0 decisions (billing, model ids, trust, secrets, doctrine,
the reliability number, untrusted content) are answered in
[docs/aria-v2/preflight.md](docs/aria-v2/preflight.md).

## Status

Phase 0 (text spine, one loop, real end-to-end) is verified: a real request
interviews → confirms → on *go* dispatches Claude Code → returns a diff with
passing tests → logs the honest outcome. Phases 1-6 (the phone call, the loop
library, mesh relocation, the house, proactivity, cloud) follow; see the plan.
