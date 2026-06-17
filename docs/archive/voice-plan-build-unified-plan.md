# Voice → Plan → Build: Unified Implementation Plan

**Single source of truth.** Hand to engineers. Implement as written. Push back on anything that doesn't make sense.

---

## TL;DR

Talk to your software development workflow from your phone. One Python process on your Mac connects Discord voice to Gemini 3.1 Live (the conversational layer), which dispatches work to Claude Opus 4.6 (the planner) and Cursor (the builder) via function calls. No other models in the loop. No conversational layers stacked on top of each other.

The whole system is glue between four SDKs. Total custom code: ~600 lines of Python + ~80 lines of Node.js. One week for the MVP if the engineer doesn't overthink it.

---

## Architectural Non-Negotiables

These are the rules. Every implementation decision must respect them. Violating any of these breaks the system.

1. **Gemini 3.1 Live is the sole conversational layer.** Voice and text. Nothing else speaks to the user. If something needs to be communicated, Gemini communicates it.

2. **Claude Opus 4.6 is the sole reasoner.** All planning, analysis, brainstorming, and architectural thinking happens here. It is invoked only as a tool. It never speaks to the user directly. Gemini summarizes Claude's output for the user.

3. **Cursor is the sole builder.** All code execution, file edits, and engineering work happens through the Cursor SDK. It is invoked only as a tool. It never speaks to the user directly. Gemini relays its progress.

4. **No Claude Sonnet anywhere.** Not for "primary model conversation," not for cheap routing, not as a fallback. If a thinking task is too small for Opus 4.6, Gemini handles it directly.

5. **No framework that owns the agent loop.** No Hermes, no LangGraph, no AutoGen. Glue, not framework. The loop is in `main.py` and you can read every line of it.

6. **Memory is delegated to a library, not built.** `mem0` for semantic/episodic memory. SQLite for structured state. You configure both; you don't reinvent either.

---

## What This System Is, in Plain English

You open Discord on your phone and join a voice channel. Gemini joins the same channel. You talk to it like a phone call. You describe a task: "Refactor the auth service. The token refresh is racy."

Gemini asks clarifying questions out loud. When it has enough context, it silently calls `plan_with_claude` — Claude Opus 4.6 produces a plan. Gemini speaks a summary; the full plan is also posted in a Discord text channel. You discuss by voice. Each adjustment goes back to Claude. When you say "ship it," Gemini calls `build_with_cursor`. Cursor edits files on your Mac. Gemini narrates progress and relays any questions. When it's done, Gemini tells you.

That's the whole system. Everything else is implementation detail.

---

## Topology

```
┌─────────────────────────────────────────────────────────┐
│  YOUR PHONE (anywhere)                                  │
│  Discord app — voice channel + text channel             │
└─────────────────────┬───────────────────────────────────┘
                      │
              Discord voice + text
                      │
┌─────────────────────▼───────────────────────────────────┐
│  YOUR MAC (always on, launchd-supervised)               │
│                                                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │  bot.py — one Python process                      │  │
│  │                                                   │  │
│  │  Discord client (discord.py)                      │  │
│  │   • voice channel: PCM capture/playback           │  │
│  │   • text channel: messages, threads               │  │
│  │                                                   │  │
│  │  Gemini Live session (google-genai)               │  │
│  │   • bidirectional audio over WebSocket            │  │
│  │   • function calling                              │  │
│  │   • conversational layer (only voice in system)   │  │
│  │                                                   │  │
│  │  Four tool handlers:                              │  │
│  │    plan_with_claude  → Anthropic API (Opus 4.6)   │  │
│  │    build_with_cursor → Node.js subprocess → SDK   │  │
│  │    query_cursor      → message running session    │  │
│  │    cursor_status     → registry query             │  │
│  │                                                   │  │
│  │  Memory (mem0) + State (SQLite)                   │  │
│  │   • mem0: user facts, project context, history   │  │
│  │   • SQLite: cursor sessions, event log            │  │
│  └────┬──────────────┬──────────────┬────────────────┘  │
│       │              │              │                   │
│  ┌────▼────┐   ┌─────▼─────┐  ┌────▼──────────────┐    │
│  │ Claude  │   │  Cursor   │  │ Local filesystem  │    │
│  │ Opus 4.6│   │  SDK (JS) │  │ (your codebases)  │    │
│  │ (cloud) │   │  → cloud  │  │                   │    │
│  │         │   │  or local │  │                   │    │
│  └─────────┘   └───────────┘  └───────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

One process. One supervisor (launchd). Three outbound API connections. Two inbound (Discord voice, Discord text). Everything else is local I/O.

---

## The Core Loop

The thirteen-step sequence the system exists to run. Nothing else.

```
 1. You speak in Discord voice:
    "Refactor the auth service. Token refresh is racy."

 2. Gemini asks clarifying questions out loud.
    "Which files? Want me to pull in refresh.ts and middleware.ts?
     Any constraints on backoff strategy?"

 3. Gemini assembles a context payload from your description and
    any files it read, then calls plan_with_claude.

 4. Claude Opus 4.6 returns a structured plan.

 5. Gemini speaks a summary. The full plan is posted to the
    Discord text channel.

 6. You respond by voice: "Looks good but add idempotency keys
    on the write path. And jitter on the backoff."

 7. Gemini calls plan_with_claude again with the prior plan +
    your feedback. Claude revises. Loop until you approve.

 8. You say "ship it." Gemini calls build_with_cursor with the
    approved plan + the implementation prompt.

 9. Cursor builds. Gemini streams progress: "Editing refresh.ts,
    now running tests."

10. If Cursor asks a question, Gemini asks you out loud, you
    answer by voice, Gemini calls query_cursor with the answer.

11. Cursor finishes. Gemini speaks the summary; the diff is in
    the text channel.
```

The system is this loop, made fast and reliable.

---

## The Stack

Every dependency below is borrowed off the shelf. You install, configure, and use. You do not extend or fork.

**Python 3.11+** — runtime.

**`discord.py` (or `py-cord`)** — Discord protocol. Handles voice channel join/leave, PCM audio receive (48kHz stereo) and send, text messages, threads, presence. Voice receive requires the voice extension; install with `pip install "discord.py[voice]"`. py-cord is a more actively maintained fork if discord.py lags; both have the same API surface for our purposes.

**`google-genai`** — Gemini 3.1 Live client. Bidirectional audio over WebSocket, function calling, session management. Native multimodal audio in/out: no STT, no TTS. Configure the model with `gemini-3.1-live` (or whichever string Google ships; verify in their docs at build time).

**`anthropic`** — Anthropic SDK. One method call per invocation: `client.messages.create(model="claude-opus-4-6", ...)`. Stateless per call; conversation history is supplied by us.

**`@cursor/sdk`** — Cursor's agent SDK. JavaScript only. We wrap it in a ~80-line Node.js subprocess that exposes a JSON-line protocol over stdin/stdout. Handles model selection (`composer-2` default), local execution in a project dir, cloud handoff, structured event streaming, MCP server connections.

**`mem0`** — OSS memory layer for AI agents. Stores user facts and conversational episodes, retrieves semantically. Configurable backend (default is local SQLite + a vector store). Hooks into Anthropic and Google models. We use it for two things: (a) cross-session context Gemini can pull on, (b) the running history Claude Opus sees during plan iteration. Install: `pip install mem0ai`.

**SQLite (stdlib)** — structured local state. One database file. Tables: `cursor_sessions` (active builds), `events` (every tool call), `discord_threads` (thread → planning session mapping). No ORM. Raw SQL.

**`python-dotenv`** — load API keys from `.env`. Never check `.env` into git.

**`numpy`** — PCM audio resampling. 48kHz stereo ↔ 16kHz mono. About four lines.

**`launchd`** — macOS process supervisor, built in. One `.plist` file. Auto-restart on crash, run at boot, redirect stdout/stderr to log files. No third-party process manager required.

That's everything. No framework. No agent orchestrator. No vector database to manage. No Kubernetes.

---

## Files

The entire project layout. Engineers can scaffold this in five minutes.

```
voice-plan-build/
├── .env                       # API keys (gitignored)
├── .env.example               # Template
├── .gitignore
├── README.md
├── requirements.txt
├── pyproject.toml             # If using poetry/uv
│
├── src/
│   ├── bot.py                 # Main entry point. The whole loop.
│   ├── discord_voice.py       # Voice channel I/O, PCM resampling
│   ├── gemini_session.py      # Gemini Live WebSocket + function calls
│   ├── tools.py               # Four tool implementations
│   ├── cursor_bridge.py       # Python side of the Node subprocess
│   ├── memory.py              # mem0 wrapper
│   ├── db.py                  # SQLite schema + queries
│   ├── prompts.py             # Loads prompt templates from disk
│   └── config.py              # Env loading, defaults
│
├── cursor_wrapper/
│   ├── package.json
│   ├── index.js               # ~80-line Node.js bridge to @cursor/sdk
│   └── node_modules/          # gitignored
│
├── prompts/                   # Markdown prompt templates
│   ├── planning.md            # Default planning prompt
│   ├── refactor.md
│   ├── architecture.md
│   ├── bug-analysis.md
│   ├── implementation.md      # Sent to Cursor with approved plans
│   └── gemini_system.md       # Gemini's system prompt (the product)
│
├── projects/
│   └── registry.md            # Name → absolute path mapping
│
├── workflows/                 # Saved workflow descriptions
│   └── plan-and-build.md
│
├── data/                      # gitignored — local state
│   ├── state.db               # SQLite
│   ├── mem0/                  # mem0's local store
│   └── events.jsonl           # Append-only event log
│
├── ops/
│   ├── com.you.voicebot.plist # launchd config
│   └── install.sh             # Setup script
│
└── tests/
    └── smoke.py               # Manual smoke tests
```

---

## The Three Intelligences — Strict Roles

### Gemini 3.1 Live — The Conversational Layer

**Does:** listen, talk, ask clarifying questions, decide when to invoke tools, summarize tool output into spoken language, manage conversation flow, handle interruptions and turn-taking natively.

**Does not:** plan, architect, write code, evaluate technical tradeoffs, run reasoning chains. When a task requires thinking, it calls `plan_with_claude`. When a task requires building, it calls `build_with_cursor`. When a question is purely conversational ("what's the weather like in plans land"), it answers directly without calling tools.

**System prompt** lives in `prompts/gemini_system.md`. This file is the product. Iterate on it relentlessly. Every behavioral problem is a prompt problem first.

### Claude Opus 4.6 — The Planner

**Does:** analyze, plan, propose, evaluate tradeoffs, structure implementation instructions, revise plans based on feedback.

**Does not:** talk to you (Gemini mediates), execute code (Cursor does), maintain its own session (we pass history in each call).

**Called by:** `plan_with_claude` tool. We pass the system prompt (one of the templates in `prompts/`), the conversation history for this planning session, and the user's latest input. We get back text. The text goes to Gemini as the tool result. Gemini speaks a summary and posts the full text to Discord.

**History per session** is keyed by Discord thread ID. Stored in SQLite. Each `plan_with_claude` call retrieves the full history and appends the new exchange.

### Cursor SDK — The Builder

**Does:** edit code, run tests, manage codebase context, execute terminal commands, fix errors mid-build, handle its own model routing internally.

**Does not:** plan (we send it an approved plan), talk to you (Gemini relays).

**Called by:** `build_with_cursor` tool. We start an Agent via the SDK with `local: { cwd: project_path }` (or cloud if specified), send the approved plan plus the implementation prompt, and stream events. Events worth surfacing: file edits, test runs, completion, questions. Everything else is logged but not narrated.

**Session registry** is in SQLite. Survives bot restart. The Cursor SDK supports reconnect for cloud sessions; local sessions die with the process (acceptable for v1).

---

## The Four Tools

This is the complete tool surface Gemini exposes. Function declarations as Gemini sees them, behavior as the handler implements.

### `plan_with_claude`

```
Parameters:
  prompt_template: string     — name of template in prompts/ (e.g. "refactor")
                                or "default" for planning.md
  context: string             — assembled context: user's request, file
                                contents, prior plan, feedback
  session_key: string         — Discord thread ID (groups related calls)

Returns: string (Claude's response)

Behavior:
  1. Load template from prompts/{prompt_template}.md
  2. Fetch conversation history for session_key from SQLite
  3. Build messages = [history..., {role: "user", content: context}]
  4. Call anthropic.messages.create(
       model="claude-opus-4-6",
       system=template_contents,
       messages=messages,
       max_tokens=8192
     )
  5. Append exchange to history in SQLite
  6. Return response text
```

### `build_with_cursor`

```
Parameters:
  project: string             — name from projects/registry.md
                                (resolved to absolute path)
  instruction: string         — approved plan + implementation prompt
  background: boolean         — default true; returns session_id immediately
                                and streams updates async

Returns: { session_id: string, initial_status: "running" }

Behavior:
  1. Resolve project name → path from registry
  2. Send a JSON command to the Node subprocess (cursor_wrapper):
     { action: "create", project_path, instruction, model: "composer-2" }
  3. Subprocess responds with session_id when Cursor session is alive
  4. Subprocess streams events on stdout (file_edit, test_run, question,
     completion, error)
  5. Each event is logged to events.jsonl and routed:
     - questions → surface to Gemini → Gemini asks user
     - completion → surface summary to Gemini
     - file_edit / test_run → throttled progress to Gemini
  6. Session row inserted in SQLite for cursor_status queries
```

### `query_cursor`

```
Parameters:
  session_id: string
  message: string

Returns: { ok: boolean }

Behavior:
  Send JSON command to Node subprocess: { action: "send", session_id, message }
  Subprocess writes to the running agent's message stream.
```

### `cursor_status`

```
Parameters: (none)

Returns: list of { session_id, project, status, started_at,
                   last_event_at, last_event_summary }

Behavior:
  SELECT * FROM cursor_sessions WHERE status IN ('running', 'waiting');
  Return as JSON.
```

Note on tool count: the v2 plan had seven tools (`call_claude`, `start_cursor`, `send_to_cursor`, `get_status`, `read_file`, `save_workflow`, `load_workflow`). We're down to four because:

- `read_file` → Gemini Live's tool system can read files directly; expose it as a built-in, not a custom tool.
- `save_workflow` / `load_workflow` → workflows live as markdown files in `workflows/`. Gemini reads and writes them via filesystem tools, no custom logic needed.

---

## Memory (using mem0)

You said you won't build memory management. You won't.

mem0 is configured once with two backends: an LLM for synthesis (Claude Opus 4.6 — though for cost reasons you might use Haiku here; flag this as a config knob) and a local vector store (Chroma is the default; lives in `data/mem0/`).

We use mem0 for two specific things:

**1. Cross-session user context.** Things like "user prefers TypeScript over JavaScript," "user's payments project lives at `~/code/payments`," "user usually wants idempotency keys on write paths." Gemini queries mem0 when assembling context for `plan_with_claude` or when answering directly. Gemini writes to mem0 when the user states a durable preference.

**2. Planning session history.** Each Discord thread is one planning session. The full message history between user and Claude lives in SQLite (raw, for exact replay). mem0 holds the semantic summary for cross-session recall ("remember the auth refactor we planned last week").

What we don't store: the raw audio (privacy + cost), full file contents (too big; we hash them and store paths).

mem0's API surface is small: `m.add(text, user_id)`, `m.search(query, user_id, limit)`, `m.get_all(user_id)`. Three calls cover all our use cases.

If mem0 turns out to be the wrong tool, swap it. The interface is small enough that the swap is a day of work. Other options: Letta (heavier, more agent-y), Zep (cloud or self-hosted), or just expanded SQLite with FTS5 + embeddings if you want zero dependencies.

---

## The Gemini System Prompt

The single most important file. Lives in `prompts/gemini_system.md`. Starting draft:

```
You are a voice assistant managing a software development workflow.
You are the only voice the user hears. You talk; everything else
is a tool you invoke silently.

You have three capabilities beyond conversation:
1. plan_with_claude — sends a planning request to Claude Opus 4.6.
   Use for any task that requires real thinking: planning, analysis,
   architecture, debugging strategy, refactor design, code review.
2. build_with_cursor — starts a Cursor agent on a project. Use only
   after an approved plan, unless the user explicitly says skip the plan.
3. query_cursor / cursor_status — talk to or check on a running build.

YOUR ROLE: You are a skilled project manager, not an architect or engineer.
- When the user describes a problem, gather what Claude needs: which project,
  which files, any constraints. Ask clarifying questions out loud.
- Do not attempt complex technical reasoning yourself. Call Claude.
- Do not write or evaluate code. That's Cursor.
- You can answer simple questions, clarify workflow, manage flow, recall
  preferences from memory.

PLANNING FLOW:
1. User describes what they want.
2. Ask clarifying questions if needed (project, files, constraints).
3. Read relevant files (built-in file_read tool).
4. Call plan_with_claude with the appropriate prompt template.
5. Speak a concise summary. Post the full plan to the text channel.
6. Ask: "Want to adjust anything, or should I send this to Cursor?"
7. If adjustments: call plan_with_claude again with prior plan + feedback.
8. If approved: call build_with_cursor with plan + implementation prompt.

BUILDING FLOW:
1. After build_with_cursor returns, monitor progress events.
2. Narrate meaningful progress (file edits, tests, completion).
3. If Cursor asks a question, ask the user, then query_cursor with the answer.
4. On completion, summarize and ask what's next.

WORKFLOW MANAGEMENT:
- If the user walks through a sequence and says "save this as [name],"
  write workflows/[name].md with the steps.
- If the user says "run [name]," read workflows/[name].md and follow it.

WHAT NOT TO DO:
- Don't read files unless needed for a Claude call or the user asked.
- Don't call Claude for trivial factual questions you can answer.
- Don't start Cursor without an approved plan (unless explicitly told to skip).
- Don't overwhelm the user with detail. Speak summaries. Post full text.
- Do not make architectural or engineering decisions. Route them to Claude.
```

This prompt is the product. Most behavioral issues are fixed here, not in code.

---

## Prompt Templates

Five markdown files in `prompts/`. Each defines a Claude system prompt for a task type.

- `planning.md` — default. Generic "produce a plan" framing.
- `refactor.md` — refactoring specifically. Asks Claude to identify primitives, list invariants, propose minimal-disruption changes.
- `architecture.md` — for new features or larger design questions.
- `bug-analysis.md` — for "something is broken, why." Asks for hypotheses ranked by probability + minimum repro.
- `implementation.md` — sent to Cursor with approved plans. Defines how Cursor should commit (branch per task, no auto-push, tests must pass).

These are the highest-leverage files in the system. Iterate on them constantly.

---

## Discord Setup Checklist

Engineers will hit this on hour one. Both prior docs skipped it.

1. **Create a Discord application** at https://discord.com/developers/applications.
2. **Add a Bot user.** Save the bot token to `.env` as `DISCORD_BOT_TOKEN`.
3. **Enable intents** in the bot settings:
   - Server Members Intent
   - Message Content Intent
   - Voice State Intent (for voice channel join/leave events)
4. **Generate an invite URL** with scopes `bot` and `applications.commands`, and permissions: View Channels, Send Messages, Create Public Threads, Send Messages in Threads, Read Message History, Connect (voice), Speak (voice), Use Voice Activity.
5. **Invite the bot to your server.**
6. **Create dedicated channels** in the server:
   - `#voice-bot` — voice channel for live conversation
   - `#bot-text` — text channel for plans, diffs, status
   - `#bot-logs` — text channel for errors and system messages (optional)
7. **Get the channel IDs** (right-click → Copy ID with developer mode on). Add to `.env`:
   - `DISCORD_VOICE_CHANNEL_ID`
   - `DISCORD_TEXT_CHANNEL_ID`
   - `DISCORD_LOG_CHANNEL_ID`
8. **Get your Discord user ID.** Add to `.env` as `AUTHORIZED_USER_IDS` (comma-separated, for single-user lock).

Bot joins voice channel on `!join` or when authorized user joins the channel (your call). Leaves on `!leave` or when channel is empty.

---

## Secrets and Config (`.env`)

```
# Discord
DISCORD_BOT_TOKEN=
DISCORD_GUILD_ID=
DISCORD_VOICE_CHANNEL_ID=
DISCORD_TEXT_CHANNEL_ID=
DISCORD_LOG_CHANNEL_ID=
AUTHORIZED_USER_IDS=

# Google (Gemini)
GOOGLE_API_KEY=

# Anthropic (Claude Opus 4.6)
ANTHROPIC_API_KEY=

# Cursor
CURSOR_API_KEY=

# Cost guardrails
DAILY_SPEND_CAP_USD=20
PER_SESSION_CLAUDE_CALLS_MAX=15
PER_SESSION_CURSOR_RUNS_MAX=5

# Behavior knobs
GEMINI_MODEL=gemini-3.1-live   # verify exact model string in google-genai docs
CLAUDE_MODEL=claude-opus-4-6
CURSOR_MODEL=composer-2
```

Never commit `.env`. Commit `.env.example` with empty values and comments.

---

## Operational Concerns

The things both prior docs handwaved that will bite you on day three.

### Cost monitoring and circuit breakers

Gemini Live in a 24/7 voice channel is a meter running. Implement:

- **Daily spend cap** — track per-API token usage in SQLite. When daily spend exceeds `DAILY_SPEND_CAP_USD`, the bot posts a warning, then refuses new tool calls until the next day or until you reset.
- **Per-session call cap** — within a single Discord thread, limit Claude calls to `PER_SESSION_CLAUDE_CALLS_MAX` and Cursor runs to `PER_SESSION_CURSOR_RUNS_MAX`. Prevents runaway loops.
- **Voice channel idle timeout** — if no one has spoken for 5 minutes, Gemini leaves the voice channel. Rejoin on demand. Saves the always-on streaming cost.

### Authorization

Only Discord user IDs in `AUTHORIZED_USER_IDS` can trigger tool calls that change state (`plan_with_claude`, `build_with_cursor`, `query_cursor`). Read-only commands (`cursor_status`) can be open or locked; default locked. Check every tool invocation against the authorized list before dispatching.

### Git hygiene for Cursor builds

The implementation prompt in `prompts/implementation.md` must instruct Cursor:

- Create a new branch for each task: `bot/<short-task-slug>-<timestamp>`.
- Commit to that branch only. Never push to `main` automatically.
- Run tests before declaring completion. If tests fail, surface to the user with the error.
- Never delete files without explicit confirmation in the plan.
- Never modify `.env`, `.git/`, or anything outside the project root.

You enforce this in the prompt, not in code. Cursor follows the prompt.

### State persistence

What survives a bot restart:

- **Cursor sessions:** cloud sessions reconnect via the SDK on startup. Local sessions die — log them as "interrupted" in SQLite and surface a message to the user on next interaction.
- **Claude conversation history:** lives in SQLite, fully persisted.
- **mem0 memory:** lives in `data/mem0/`, fully persisted.
- **Discord state:** Discord is the source of truth for messages and threads; the bot rejoins channels on startup.

What does not survive:

- The current Gemini Live session (the audio conversation). Gemini Live sessions are ephemeral. On restart, the bot rejoins the voice channel if you're in it, and a new session begins.

### Process supervision (launchd)

One `.plist` file at `ops/com.you.voicebot.plist`. Loaded with `launchctl bootstrap gui/$(id -u) ops/com.you.voicebot.plist`. Configures:

- Run at user login.
- KeepAlive (restart on crash).
- StandardOutPath / StandardErrorPath to `~/Library/Logs/voicebot/`.
- Working directory to the project root.
- ProgramArguments to invoke `python -m src.bot`.

Skeleton plist is in the `ops/` directory.

### External health check

Once a minute, the bot pings a free external uptime monitor (healthchecks.io has a free tier). If the bot stops pinging, the monitor texts you. This is how you find out your bot has been dead for two hours without checking Discord.

### Backups

Daily rclone or restic of `data/` to a cloud bucket. Workspace contains memory, event log, session registry. Losing the Mac shouldn't lose the brain. One cron line.

### Logging

Two streams:

- **events.jsonl** — append-only, one line per tool call. Schema: timestamp, tool name, params summary, result summary, duration_ms, session context. This is your training data if you ever want it.
- **stderr** — standard Python logging to a rotating file. INFO by default; DEBUG when you're chasing a bug.

---

## Build Phases

Aggressive timeline. Engineers should not need more.

### Phase 0 — Setup (½ day)

- Discord bot created, invited, channels configured.
- API keys in `.env`, all four (Google, Anthropic, Cursor, Discord).
- Python project scaffolded; `requirements.txt` installs cleanly.
- Node `cursor_wrapper/` scaffolded; `node index.js --healthcheck` returns OK.
- launchd plist installed; bot starts on login.

**Gate:** bot logs in to Discord, joins the voice channel on command, leaves on command. No audio, no tools yet.

### Phase 1 — Voice loop (3-4 days)

- `discord_voice.py`: PCM capture, resample 48k stereo → 16k mono. Playback 16k mono → 48k stereo.
- `gemini_session.py`: open Gemini Live WebSocket, stream audio bidirectionally. Define an empty tool list. Wire system prompt from `prompts/gemini_system.md`.
- Test: join voice channel from phone, speak, hear Gemini reply with natural conversation. Verify latency under 1 second for direct responses.

**Gate:** fluid voice conversation through Discord with no tools. Gemini handles turn-taking and interruptions natively.

### Phase 2 — Plan and build (3-4 days)

- `tools.py`: implement all four tool handlers. Wire Gemini function declarations.
- `prompts.py`: load templates from disk on each call.
- `db.py`: schema and queries for cursor_sessions, events, planning history.
- `memory.py`: mem0 wrapper. Initialize on bot startup; expose `remember`, `recall`, `forget`.
- `cursor_wrapper/index.js`: ~80 lines. Reads JSON commands on stdin, writes events on stdout. Manages Cursor agent lifecycle.
- Wire text-channel posting: full plans, diffs, status updates post in `#bot-text` in parallel with voice.
- Wire thread creation: each `build_with_cursor` call creates a Discord thread. Progress events post in that thread.

**Gate:** speak a real task, hear the agent gather context, hear it ask if it can call Claude, hear the summary spoken (full plan in text), iterate by voice, approve, watch Cursor build on the Mac, hear completion. Run this on a real project end to end.

### Phase 3 — Polish and harden (2-3 days)

- Error recovery: Gemini WebSocket drops, Claude API timeout, Cursor agent crash, Discord disconnect. Each gets a simple recovery path (reconnect with backoff, surface to user, log).
- Parallel Cursor sessions: two threads, two active builds. Verify `cursor_status` lists both, `query_cursor` routes correctly.
- Cost circuit breakers active and tested.
- Authorization checks on every state-changing tool call.
- launchd auto-restart verified by `kill -9`.
- External health check pinging.
- Backup cron set up.

**Gate:** use it for a full day of real work. Three tasks across two projects. At least one failure that recovers gracefully. Come back from lunch to a completed build with a question waiting in voice.

### Phase 4 — Saved workflows and prompt iteration (ongoing)

The system is complete. From here, the work is:

- Writing better prompt templates. Highest leverage.
- Saving workflows as you discover repeated patterns. Each save makes the next invocation faster.
- Tuning the Gemini system prompt for behaviors you want adjusted.

No more code. Markdown only.

---

## What Is Explicitly Not Built

Hold the line on these. They are out of scope for v1.

**No agent framework.** No Hermes, no LangChain, no AutoGen. The loop is in `main.py`.

**No second conversational layer.** No Claude Sonnet "primary model." Gemini is the only voice.

**No vector database service.** mem0's local store is enough. If you need scale later, swap mem0's backend, not the architecture.

**No web UI / admin panel.** Discord is the UI.

**No multi-user.** Single authorized user list. If you want multi-user later, scope it to a feature, not a refactor.

**No constructor loop / pattern detection.** The v2 plan deferred this. The Hermes plan elaborated it. We defer it. The event log captures everything; if you later want automated pattern detection, the data is there. Don't build it speculatively.

**No GPU / local model serving.** Cloud APIs only. When the Nvidia GPU arrives, you optionally add local Whisper (audio backup if Gemini Live drops) or a local cheap router. Both are additive, neither blocks v1.

**No PC handoff.** Everything runs on the Mac. The codebase is on the Mac. Cursor builds on the Mac. The PC is irrelevant to v1.

**No production deployment / sandbox.** This runs on your personal Mac, on your code, under your control. When you want to expose it to teammates or run it on third-party code, you sandbox then. Not now.

---

## Engineer Handoff Checklist

What an engineer must produce, in order.

1. Scaffold the project (`requirements.txt`, `pyproject.toml`, directory structure).
2. Get Phase 0 green: bot logs in, joins/leaves voice channel.
3. Implement `discord_voice.py` and `gemini_session.py`. Phase 1 gate: fluid voice conversation.
4. Implement `db.py`, `memory.py`, `cursor_bridge.py`, `cursor_wrapper/index.js`.
5. Implement the four tools in `tools.py`. Wire function declarations into Gemini session.
6. Write the prompt templates in `prompts/`. Iterate the Gemini system prompt as behavior emerges.
7. Phase 2 gate: end-to-end plan-and-build via voice on a real project.
8. Error recovery paths for the four failure modes (Gemini drop, Claude timeout, Cursor crash, Discord disconnect).
9. Cost circuit breakers and authorization checks.
10. launchd plist, health check, backup cron.
11. Phase 3 gate: full day of real-work use without intervention.

Code count target: ~600 lines Python + ~80 lines JS. If your engineer is writing more, they're building a framework. Push back.

---

## Open Questions to Confirm

Flag these to the user before starting Phase 2.

1. **mem0 backend LLM.** mem0 uses an LLM to synthesize memories. Default config uses the same model you query with. Confirm: use Opus 4.6 (highest quality, higher cost) or Haiku 4.5 (much cheaper, slightly lower quality summaries)? I recommend Haiku 4.5 for this specific role since memory synthesis doesn't need Opus's reasoning.

2. **Cursor execution location.** Default is local on your Mac (`local: { cwd: project_path }`). Cursor SDK also supports cloud sandboxes. Confirm: local-only for v1, or wire the cloud option from day one with a flag?

3. **Voice channel auto-join vs. on-command.** Should the bot join the voice channel automatically when you join, or wait for an explicit `!join` command? Auto-join is more phone-call-like; on-command is cheaper and gives you a clean "off" state.

4. **What model does Gemini fall back to if Live API is unavailable?** Options: error out (simplest), fall back to Gemini Pro with separate STT/TTS (more code, less native), text-only mode (graceful degradation). I recommend error out for v1.

5. **Push-to-talk or open mic?** Default is open mic (Gemini Live handles VAD natively). Push-to-talk is more controlled but less natural. Confirm: open mic for v1, revisit if it's too chatty.

6. **Daily spend cap.** $20/day suggested in `.env.example`. Confirm your tolerance.

---

## Why This Is Right

Three intelligences, one conversation surface, strict roles, no extra layers.

- One process you can read end to end.
- Three SDKs that each do one thing, well-supported by their vendors.
- One OSS memory library you configure but don't extend.
- One supervisor that's already on your Mac.
- One codebase location (your Mac), no remote handoff.

The system is small enough to hold in your head. That's the design constraint. When you find yourself adding a "primary model," a "router agent," a "memory manager service," or an "orchestration framework," stop. The architecture is already serving the use case. Add complexity only when something concrete breaks.
