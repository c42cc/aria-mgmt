# Aria

**A voice-first personal operating shell.** You talk to a Discord bot named
**Aria** from your phone. She speaks back conversationally and, behind the
scenes, dispatches real work — planning, building software, reading mail,
managing files, running shell commands, watching your Cursor windows,
recalling durable facts — through a fleet of specialized intelligences.

Everything runs as **one Python process on your Mac**, plus a few subprocess
sidecars for what Python can't own directly. No agent framework, no
orchestrator. The loop is readable end-to-end in `src/bot.py`.

---

## What Aria is

Aria appears in your Discord server as a single bot. She is the only non-human
in the server and manages every channel and thread herself. Under the hood she
is **three intelligences with strict, non-overlapping roles**:

| Role | Who | What it does |
|---|---|---|
| **Voice** | Gemini Live | The only thing you hear. Listens, speaks, decides which tool to call. Does no reasoning beyond conversation. |
| **Reasoning** | Claude Opus | Planning, analysis, the multi-step agent loop. Never speaks to you directly; produces text artifacts. |
| **Building** | Cursor SDK | Code edits, tests, branches. Never narrates; emits build events. |

Roles never blur — that is why the system stays small. For the why and where
this is going, read [`VISION_ARIA.md`](./VISION_ARIA.md).

## How she works

- **One process** owns the conversation, tool dispatch, state, memory, and the
  supervision of every sidecar. launchd brings it back if it dies.
- **Sidecars** speak line-delimited JSON over stdio: a Node **discord.js**
  voice bridge (Discord voice E2EE), a Node **`@cursor/sdk`** wrapper, and a
  fleet of **MCP servers** (one per integration: Apple, Google Calendar,
  filesystem, shell, GitHub, …).
- **Tools** are the action surface. Gemini doesn't touch the world directly; it
  calls Python tool handlers on a fixed catalog, with cost and risk-tier checks
  applied mechanically at dispatch.
- **Prompts are behavior, and that behavior is a program she edits.** Every
  persona/instruction is a markdown file in `prompts/`, read at runtime. That
  library — plus the injection, version control, and eval loop that operate on
  it — is the **Universal Constructor** ([`src/constructor/`](src/constructor/)),
  the inspectable program Aria *wields*. She edits, versions, and rolls back her
  own prompts by voice; a change takes effect on the next call. The Constructor
  is a subsystem Aria uses, not Aria herself — see
  [`VISION_CONSTRUCTOR.md`](./VISION_CONSTRUCTOR.md).
- **Failures are loud.** Preflight probes every capability end-to-end and
  refuses to enter ready state on a critical failure, with the exact fix.

Full technical map: [`ARCHITECTURE.md`](./ARCHITECTURE.md). Process topology,
IPC, audio formats, and lifecycle wiring: [`wiring.md`](./wiring.md).

## What she can do

- **Talk** — natural voice conversation in the voice channel.
- **Plan with Claude** — describe a problem; hear a plan; approve/adjust/reject.
- **Build with Cursor** — on approval, spin up a Cursor agent on a fresh branch
  and stream the build back.
- **Do with Claude (MCP)** — email, calendar, files, research, shell, GitHub via
  one bounded agent loop with the MCP catalog wired in.
- **Watch your Cursor windows (cursor-watch)** — Aria sees every Cursor IDE
  thread on the Mac and pings you when one needs a decision or finishes.
- **Remember & recall** — durable facts persist in mem0 and load into context.
- **Manage the Sparks** — status/verify/setup for the two-node DGX Spark rig.
- **SpicyLit** — a capability that swaps the voice layer to a storyteller in its
  own channel.
- **Local voice** — same tools with the Mac's mic/speakers, no Discord.

---

## Quick start

```bash
# 1. Python env + editable install (src/ is imported live)
python3 -m venv .venv
source .venv/bin/activate
pip install -e .            # or: pip install -r requirements.txt

# 2. Node sidecars
(cd cursor_wrapper && npm install)
(cd discord_voice_bridge && npm install)

# 3. Secrets
cp .env.example .env        # then fill in keys + Discord/channel IDs

# 4. Install the cursor-watch hooks (lets Aria see your Cursor windows)
.venv/bin/python hooks/install.py

# 5. Run (development)
make run                    # or: python -m src.bot
```

A fresh machine can use `ops/bootstrap.sh` (deps + sidecars + permissions).
`deploy.sh` is the production launch path; `kill.sh` tears down the whole
process tree (launchd-aware). The launchd unit is `ops/com.you.voicebot.plist`.

## Discord setup

Aria is **one identity, two Discord applications** sharing the name "Aria":

- **Text bot** (py-cord) — text commands, threads, tool dispatch.
  `DISCORD_APP_BOT_TOKEN`.
- **Voice sidecar** (discord.js) — only the voice WebSocket.
  `DISCORD_VOICE_BOT_TOKEN`.

They never talk through Discord; the Python parent is the only glue.

Channels (referenced by **ID**, not name — renaming in Discord never breaks
anything):

| Channel | Kind | Purpose | `.env` |
|---|---|---|---|
| `#general` | voice | Conversation with Aria | `DISCORD_VOICE_CHANNEL_ID` |
| `#ucs` | text | Plans, build output, results, decisions/buzzes | `DISCORD_TEXT_CHANNEL_ID` |
| `#ucs-alerts` | text | Silent audit stream (preflight, errors, cursor-watch) | `DISCORD_LOG_CHANNEL_ID` |
| `#spicy-lit` | voice | SpicyLit storyteller capability | `DISCORD_SPICYLIT_CHANNEL_ID` |

`.env.example` is the canonical list of recognized variables.

## Repo layout

```
src/            # Python source — the loop lives in src/bot.py
cursor_wrapper/         # Node bridge to @cursor/sdk
discord_voice_bridge/   # Node discord.js voice sidecar
hooks/          # ~/.cursor/hooks.json forwarder for cursor-watch
capabilities/   # Self-contained capability packages (e.g. spicy_lit)
prompts/        # Markdown prompt templates (behavior is data)
projects/       # Project name -> path registry
specs/          # Correctness specs (judge harness)
ops/            # launchd unit, bootstrap/install, Spark ops
scripts/        # E2E + acceptance harnesses
tests/          # Unit tests + deep integration
docs/           # Reference docs, forensics, and archived planning briefs
data/           # Local state — SQLite, mem0, audit log (gitignored)
models.yaml     # Model registry + loop profiles
```

A concept-to-file index lives in [`ARCHITECTURE.md`](./ARCHITECTURE.md)'s
**Repo Map** — start there when you need to find where something lives.

## Documentation

| Doc | What it is |
|---|---|
| [`README.md`](./README.md) | This file — what Aria is and how to run her |
| [`VISION_ARIA.md`](./VISION_ARIA.md) | What Aria is and where she's going |
| [`VISION_CONSTRUCTOR.md`](./VISION_CONSTRUCTOR.md) | The Universal Constructor — the prompt library + injection + eval engine Aria wields |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | Primitives, layers, capabilities, repo map |
| [`wiring.md`](./wiring.md) | Process topology, IPC, audio, lifecycle wiring |
| [`docs/`](./docs/) | Reference docs, forensics, archived planning briefs |
