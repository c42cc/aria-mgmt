# UCS Architecture

A map for a senior engineer with full repo access and limited memory of the system.
This document describes what UCS is, the primitives it stands on, the layers it
runs through, the capabilities it exposes, and where each thing lives in the repo.

For process topology, IPC, audio formats, and lifecycle wiring, read
[`wiring.md`](./wiring.md).

---

## Purpose

UCS is a voice-first personal operating shell. The user talks to a Discord bot
named **Aria** from a phone. Aria speaks back conversationally and, behind the
scenes, dispatches real work — planning, building software, reading mail,
managing files, running shell commands, recalling durable facts — through a
fleet of specialized intelligences.

The headline use case is voice-driven software development: describe a problem
out loud, hear a plan, approve it, hear the build narrate itself, get the diff
in a text channel. But the same shell handles general assistant work via MCP.

The whole system is **one Python process on the user's Mac**, plus a small
number of subprocess sidecars for what Python cannot own directly. launchd
can supervise it; `deploy.sh` is the production launch path; `make run` is
the development path. There is no agent framework, no orchestrator, no
meta-router. The loop is readable end-to-end in `src/bot.py`.

---

## Primitives

The system is built on a small set of stable abstractions. Everything else is
combination and configuration.

### One Process

A single long-running Python process owns the conversation, the tool dispatch,
the state, the memory, and the supervision of all sidecars. When this process
dies, everything dies with it; launchd brings it back. There is no broker, no
queue server, no separate worker.

### Three Roles, Strict Separation

| Role | Identity | What it does |
|---|---|---|
| Voice | **Gemini Live** (or a capability-specific replacement) | The only thing the user hears. Listens, speaks, decides which tool to invoke. Does no reasoning beyond conversational flow. |
| Reasoning | **Claude Opus** | Planning, analysis, multi-step tool execution. Never speaks to the user directly. Posts text artifacts to the text channel. |
| Building | **Cursor SDK** | Code edits, tests, branch creation. Never speaks to the user directly. Streams build events. |

Roles do not blur. Gemini does not reason; it routes. Claude does not speak;
it produces text. Cursor does not narrate; it emits events.

### Sidecars for What Python Cannot Own

Three classes of sidecar, each chosen because Python cannot own the protocol
natively:

- **Discord voice sidecar** — a Node process running discord.js, because the
  Python Discord libraries do not implement Discord's mandatory voice E2EE
  protocol. Owns the voice WebSocket and nothing else.
- **Cursor wrapper** — a Node process running `@cursor/sdk`, because the
  Cursor SDK is JavaScript.
- **MCP servers** — third-party processes (Node or Python) speaking the Model
  Context Protocol over stdio. Each one owns one integration (Apple, Google
  Calendar, filesystem, shell, GitHub, …).

All sidecars speak **line-delimited JSON over stdio**. The Python parent owns
their lifecycle; if Python dies, they die.

### Tools as the Action Surface

The conversational layer (Gemini) does not act on the world directly. It calls
**tools**, which are Python functions on a fixed catalog. The catalog is the
contract: changing tool shapes is a deliberate, declared change.

Tools fall into a few flavors:

- **Reasoners** that call Claude with a chosen prompt template.
- **Builders** that hand a plan to Cursor and stream events back.
- **MCP-backed actions** that route through the MCP fleet (the `do_with_claude`
  agent loop and a few read-only shortcuts).
- **Memory** to durably remember and recall facts.
- **Control** to confirm or cancel work in flight.
- **Self-management** to list, view, edit, and reload the prompt templates
  themselves so the user can tune behavior by voice.

### Prompts as Editable Behavior

Every persona and every reusable instruction is a markdown template in
`prompts/`. The bot reads them at runtime. The user can ask Aria to edit her
own system prompt, or any other persona; the bot edits the file, reloads,
reconnects. Behavior is data, not code.

### State as Three Flat Stores

There is no general-purpose data layer. State lives in three concrete places:

- **SQLite** (`data/state.db`) for structured rows: cursor session lifecycle,
  tool events with cost, planning history per session, thread bindings, and
  capability-owned tables (e.g., SpicyLit outlines).
- **mem0 vector store** (`data/mem0/`) for durable user-stated facts.
- **An append-only audit log** (`data/audit.jsonl`) recording every MCP tool
  call with arguments redacted by type, the tier, and the confirmation
  outcome.

### Capabilities

Anything that is not core to the voice-plan-build loop lives under
`capabilities/`. A capability is a self-contained package with its own
voice/text pipeline, its own LLM provider if needed, and its own tables in
the shared SQLite. It is wired into `bot.py` by channel — when the user enters
a particular Discord voice channel, the bot routes audio to that capability's
session instead of Gemini.

The SpicyLit capability is the reference example.

### Preflight as the Gate

Before the bot accepts any voice command, it runs a battery of capability
probes — each one performs a real round-trip against the real subsystem. The
report posts to the alerts channel. If a critical probe fails, the bot
refuses to enter ready state and refuses to join voice. There are no silent
fallbacks; failures are surfaced with the exact command to fix them.

### Risk Tiers, Mechanically Enforced

MCP tools are classified by the dispatcher into a small set of tiers (read,
write, irreversible, executable). Irreversible and executable tiers
**always** require a confirmation round-trip through the user before they
fire. This is not advisory; it is mechanical, applied at the dispatch point.
The confirmation flow uses the voice channel to ask and the function-calling
surface to record the answer.

### Cost Guardrails

A daily spend ceiling, per-session call limits, and a per-task iteration cap
all live in `.env` and are enforced at the tool dispatcher. When the ceiling
is hit, the tool returns an error instead of calling the API. The bot
continues to function on free operations (status, recall, cancel, prompt
inspection) so the user can investigate.

---

## Layers

Top-down. Each layer talks only to its immediate neighbors.

### Transport

Discord voice channel and Discord text channels. The voice transport is
isolated in the discord.js sidecar; everything that is not voice WebSocket
goes through py-cord inside the main process. The split exists because of
Discord's voice E2EE; otherwise it would be one library.

There are several channels with distinct purposes: a voice channel for
conversation with Aria, a text channel for plans/results/diffs, an alerts
channel for system events and preflight reports, and capability-specific
channels (e.g., the SpicyLit voice channel that switches the audio pipeline
to a different model).

### Conversational

Gemini Live, by default. Holds the WebSocket, streams audio in both
directions, owns input/output transcription, exposes the tool catalog, and
dispatches function calls to the Python tool handler.

A capability may replace this layer for the duration of a session. SpicyLit
swaps Gemini for Grok's Voice Agent API when the user is in the SpicyLit
voice channel; the rest of the bot is unchanged.

The voice session is **resilient and ephemeral**. Idle silence pauses the
session and saves a short transcript context. The next utterance from the
user reconnects and replays the context as background. Total silence over a
longer window leaves the voice channel entirely.

### Tool Dispatch

A flat dispatch table maps tool names to Python handlers, with cost and
risk-tier checks applied uniformly before any handler runs. Every dispatch
is timed and recorded in the events table; significant ones carry an
estimated dollar cost.

### Reasoning & Action

Three concrete patterns sit behind the tool dispatch:

- **Single-shot Claude** for planning: send a prompt template plus context,
  receive markdown, post to the text channel, log cost. Planning history is
  retained per session so iterative refinement works.
- **Iterative Claude with MCP tools** for general tasks: an agent loop bounded
  by iteration count and token budget, where Claude calls MCP tools and
  observes their results until done.
- **Cursor agent** for builds: hand the approved plan and an implementation
  persona to the Cursor SDK in a project working directory; stream the
  resulting events back into Discord and into Gemini's context.

When `UCS_ENABLED=true`, the first two patterns are served by the UCS
Intelligence Loop (`src/ucs.py`) instead of direct Anthropic calls. The
loop supports model hot-swapping per step, context budget management, and
a configurable iteration cap. The Cursor agent path is unchanged. Both
paths write to the `loop_executions` table for observability.

### State

Three flat stores, listed in Primitives. None of them are abstracted behind
an ORM. SQL is written directly. mem0 is used through its own client. The
audit log is appended line by line.

### Boot & Health

Configuration is loaded once at import time from `.env` into a frozen
dataclass. Preflight runs immediately after Discord login and before voice
is enabled. Health is observable through the `!status` command and through
the alerts channel.

---

## Capabilities

The user-facing things the system can do today.

### Talk to Aria

The default mode. The user joins the conversation voice channel; Aria joins,
greets, and listens. Aria asks clarifying questions out loud, narrates work
in progress, and routes everything substantive to a tool.

### Plan with Claude

The user describes a software problem. Aria gathers the right context,
chooses an appropriate prompt template (planning, refactor, architecture,
bug analysis), and sends a single Claude call. The plan posts to the text
channel; Aria speaks a summary and asks for adjustments or approval.
Iterations chain through the planning history table so each call sees the
prior turns.

### Build with Cursor

After an approved plan, Aria starts a Cursor agent on a project from the
registry, on a fresh branch, with the implementation persona prepended. A
build thread is created in the text channel. Cursor events stream into that
thread; on completion or error, Aria narrates the outcome.

### Do with Claude

For anything that isn't pure software planning — email, calendar, files,
research, shell, GitHub — Aria hands the task to an iterative Claude agent
with the MCP tool catalog wired in. The agent loops, calling tools and
observing results, until it finishes or hits its iteration/token bound.
Risk-tier confirmations surface to the user mid-loop when needed.

### Quick Read Shortcuts

Common read-only questions ("any new mail?", "what's on my calendar?")
bypass the full Claude loop and hit the relevant MCP tool directly,
returning fast.

### Remember & Recall

Durable facts the user states ("I'm allergic to peanuts", "my CTO is named
Mike") get stored in mem0. Future planning and Claude-agent calls pull
relevant memories into the prompt context automatically.

### Prompt Self-Management

The user can ask Aria, by voice, to list available prompts, show one, or
edit one with a natural-language change. Claude applies the edit; the new
version posts to the text channel for review; Aria's own system prompt can
be reloaded live so changes take effect without a process restart.

### Cancel & Confirm

`!stop`, or a spoken "stop / cancel / abort / nevermind", aborts the current
build or agent loop. Risk-tier confirmations pause the agent and ask the
user out loud; the user's verbal yes/no is captured by Gemini and resolved
back into the agent's wait condition.

### SpicyLit

A capability that replaces the voice layer in a designated channel. Joining
that channel hands audio to a Grok Voice session with a storyteller
persona. The Grok session collects preferences, calls an outline-generation
function, persists the outline, posts it to the SpicyLit text channel, and
then narrates the story. Pause/resume and total-silence-exit work the same
as the Gemini path.

### Local Voice (no Discord)

An alternative entry point that uses the Mac's microphone and speakers
directly instead of Discord. Same tool surface, same prompts, same MCP
fleet — only the transport is different. Useful for debugging and for
running offline of Discord.

### External Cursor Observer

Aria's eyes on the Cursor IDE windows the user opened manually (not
spawned by `build_with_cursor`). A small aiohttp server bound to
127.0.0.1 receives Cursor lifecycle events from a user-level hooks
forwarder (`hooks/cursor-event.py` registered in `~/.cursor/hooks.json`).
The observer filters/debounces, reads transcript JSONLs and plan files
on disk for context, and hands interesting events to a pager.

Pager rungs:

- **Rung A** — Gemini is connected: `inject_text(turn_complete=True)`,
  Aria narrates the event aloud immediately.
- **Rung B** — Gemini is not connected: Discord DM with `<@USER_ID>`
  mention. The event is queued; when the user later joins voice, the
  join preamble is replaced with a debrief instruction so Aria opens
  the conversation with a one-sentence summary instead of "stay silent
  until he speaks."

The Cursor remote-control tools (`list_cursor_windows`,
`read_cursor_window`, `focus_cursor_window`, `send_to_cursor_chat`,
`approve_cursor_plan`, etc.) let Aria be the user's "body" on the
workstation: she can read what each window is doing and type
instructions back into specific windows by AppleScript-focusing them
and pasting into the chat sidebar.

Discord's bot API does not permit DM voice-call ringing; the DM-mention
push is the loudest legal proxy. Pushover/Twilio escalation can bolt
onto the same pager interface later if more aggressive ring is needed.

---

## Repo Map

Use this to find where a concept lives. Treat it as a directory of starting
points; cross-references inside the code are reliable.

### Entry points

| What | Where |
|---|---|
| The main loop | `src/bot.py` |
| The Mac-local loop (no Discord) | `src/local_voice.py` |
| The boot health checks | `src/preflight.py` |
| Bootstrap a fresh machine | `ops/bootstrap.sh` |
| Run (development — foreground, simple kill) | `make run` via `Makefile` |
| Deploy (production — git push, restart, smoke test) | `deploy.sh` |
| Kill all processes (launchd-aware) | `kill.sh` |
| launchd unit | `ops/com.you.voicebot.plist` |

### Layers

| Layer | Where |
|---|---|
| Discord voice transport (Python side) | `src/discord_voice.py` |
| Discord voice transport (Node sidecar) | `discord_voice_bridge/index.js` |
| Discord text/commands/threads | `src/bot.py` (py-cord usage) |
| Conversational session (default) | `src/gemini_session.py` |
| Conversational session (SpicyLit) | `capabilities/spicy_lit/grok_voice.py` |
| Tool dispatch & tool implementations | `src/tools.py` |
| UCS intelligence loop + model router | `src/ucs.py` (active when `UCS_ENABLED=true`) |
| UCS evaluation layer (offline CLI) | `src/eval.py` |
| Product correctness harness | `src/judge.py` + `specs/correctness/` |
| MCP client and dispatch | `src/mcp.py` |
| Cursor bridge (Python side) | `src/cursor_bridge.py` |
| Cursor bridge (Node sidecar) | `cursor_wrapper/index.js` |
| External Cursor observer | `src/cursor_external.py` (HTTP server + transcript reader + pager dispatch) |
| Cursor hooks forwarder | `hooks/cursor-event.py` (+ `hooks/install.py`) |
| Long-term memory | `src/memory.py` |
| Structured state | `src/db.py` + `data/state.db` |
| Audit log | `src/mcp.py` (writer) + `data/audit.jsonl` |
| Configuration | `src/config.py` + `.env` |
| Model registry | `models.yaml` |

### Editable behavior

| What | Where |
|---|---|
| Aria's system prompt | `prompts/gemini_system.md` |
| Claude planning personas | `prompts/{planning,architecture,refactor,bug-analysis}.md` |
| Claude implementation persona | `prompts/implementation.md` |
| Claude general-agent persona | `prompts/do_with_claude_system.md` |
| Build target paths | `projects/registry.md` |
| Saved workflows | `workflows/` |
| Model registry + loop profiles | `models.yaml` |

### Capabilities

| What | Where |
|---|---|
| Capability framework convention | `capabilities/` (each package is self-contained) |
| SpicyLit (Grok voice + outline pipeline) | `capabilities/spicy_lit/` |

### Tests & ops

| What | Where |
|---|---|
| Smoke tests (cheap, no API calls) | `tests/smoke.py` |
| Deep integration (real API round-trips) | `tests/deep_integration.py` |
| One-shot machine setup | `ops/bootstrap.sh` |
| Dependency install (venv + npm) | `ops/install.sh` |
| macOS permissions helper | `ops/grant_permissions.sh` |
| Swift binary build for Apple MCP | `ops/build_macos_swift.sh` |
| Google OAuth bootstrap | `ops/google_oauth_bootstrap.py` |
| DGX Spark node setup (Section A, user-level, idempotent) | `ops/spark/setup_node.sh` |
| DGX Spark acceptance harness (capture + Gemini visual verify) | `scripts/spark_acceptance.py` |
| DGX Spark ops notes + future Aria capability design | `ops/spark/NODES.md` |

### Runtime state (gitignored)

| What | Where |
|---|---|
| Structured state DB | `data/state.db` |
| Memory vector store | `data/mem0/` |
| MCP call audit | `data/audit.jsonl` |
| Correctness verdicts | `data/verdicts.ndjson` |
| Stale-launch sentinel | `data/.preflight_boot_sha` |

---

## Fundamentals

Things to assume true when reading any module. If the code seems to violate
one of these, treat that as a bug, not a feature.

1. **The loop is readable in `bot.py`.** Every other module is a peripheral.
   If you need to know what happens when audio arrives, when a tool is
   called, when the user joins voice, when a build event lands — start in
   `bot.py` and follow the call.

2. **Gemini speaks. Claude thinks. Cursor builds.** A change that asks one
   role to do another's job is a design regression. The roles are why this
   system is small.

3. **Failures are loud.** No silent fallbacks anywhere. If a probe fails,
   preflight refuses to enter ready. If an MCP server crashes, the
   dispatcher surfaces the error. If Gemini drops, it backs off and
   reconnects with prior transcript context. If anything is wrapped in
   `try/except: pass`, that is a bug.

4. **Aria is one identity, two Discord applications.** One is the text/
   commands bot (py-cord); one is the voice WebSocket bot (discord.js).
   They never communicate through Discord; the Python parent process is the
   only glue. To the user there is one bot.

5. **Channels are identified by ID, not name.** Renaming a channel in
   Discord never breaks the bot. Channel IDs are configured via `.env`.

6. **Prompts are part of behavior, not docs.** Editing a file in `prompts/`
   changes how the system acts on the next read. Cached templates are
   invalidated explicitly via the reload tool or the `!reload` command.

7. **Risk tiers are mechanical.** Every MCP tool call is classified by the
   dispatcher; irreversible and executable tools always confirm before
   they run. This is not enforced by prompting Claude to be careful.

8. **Cost discipline is mechanical.** The daily ceiling, per-session call
   limits, and iteration caps live in config. The dispatcher checks them
   before delegating; a refusal returns a JSON error instead of calling
   the API.

9. **The audit log is the ground truth for sensitive actions.** Anything
   that goes through the MCP dispatcher is recorded with arguments
   redacted by type, tier, confirmation outcome, and result summary. When
   investigating "did the bot really send that?", read the audit log.

10. **Capabilities plug in by channel.** When you add a new capability,
    you do not modify the core tool catalog. You add a package under
    `capabilities/`, wire it to a channel ID, and let `bot.py` route audio
    to its session when the user enters that channel.

11. **The Mac is the boundary.** Cursor runs on the Mac. The code is on
    the Mac. The bot runs on the Mac. There is no remote worker, no PC
    handoff, no GPU dependency.

12. **Both launch paths ensure fresh code.** `make run` (development) and
    `deploy.sh` (production) both reinstall the package in editable mode
    before launching, so the running code always matches the source on
    disk. The preflight `running_code` probe verifies this at boot.

13. **User voice edits to prompts always win.** The eval layer advises; it
    does not override. When a user tunes a prompt by voice through the
    Universal Constructor loop, that edit is the new ground truth. The eval
    layer may flag that a user edit degraded a score, but it must never
    automatically roll back or overwrite a user-initiated change. Trust in
    the system depends on this guarantee.
