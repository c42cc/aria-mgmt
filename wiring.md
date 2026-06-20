# UCS Wiring

How the pieces named in [`ARCHITECTURE.md`](./ARCHITECTURE.md) actually connect:
process topology, IPC protocols, audio direction conventions, callback
registration, and the runtime lifecycle from boot to shutdown.

Read this when you need to know *how data crosses a boundary*. Read
`ARCHITECTURE.md` when you need to know *what something is*.

---

## Process Topology

```
                          launchd
                             │
                             ▼
                ┌─────────────────────────┐
                │   Python parent (bot)   │
                │   src/bot.py            │
                └────┬─────────┬──────────┘
                     │         │
       ┌─────────────┘         └──────────────┬──────────────┐
       │                                      │              │
       ▼                                      ▼              ▼
┌──────────────┐                  ┌─────────────────┐ ┌──────────────┐
│ Node sidecar │                  │ Node sidecar    │ │ MCP servers  │
│ Discord voice│                  │ Cursor SDK      │ │ (stdio, one  │
│  discord.js  │                  │  @cursor/sdk    │ │  per         │
│              │                  │                 │ │  integration)│
└──────┬───────┘                  └─────────────────┘ └──────────────┘
       │
       ▼
 Discord voice WebSocket          Cursor agents              Apple, GCal,
 (DAVE E2EE)                      (local or cloud)           filesystem,
                                                             shell, GitHub
```

The Python parent additionally holds:

- A long-lived WebSocket to **Gemini Live** (the conversational layer) or, for
  capability sessions, to **Grok Voice Agent API**.
- Outbound HTTPS to **Anthropic** for Claude calls.
- The **py-cord** Discord client for text channels, commands, threads, and
  voice state events. This is in-process; it does **not** own voice audio.

Every sidecar is a child of the Python parent. Killing the parent kills the
children. Sidecars are not expected to be restarted independently; the parent
restarts via launchd on crash.

---

## IPC Protocols

All sidecars speak **line-delimited JSON over stdio**, one JSON object per
line on stdin (parent → child) and stdout (child → parent). stderr is drained
and surfaced at debug level.

### Cursor wrapper (`cursor_wrapper/index.js`)

Multiplexed: responses and events ride the same stdout stream, demuxed by
the Python side via `request_id`.

Outbound (Python → Node):

```
{ "request_id": "<uuid>", "action": "ping" }
{ "request_id": "<uuid>", "action": "create", "project_path": "...", "instruction": "...", "model": "..." }
{ "request_id": "<uuid>", "action": "send",   "session_id": "...", "message": "..." }
{ "request_id": "<uuid>", "action": "cancel", "session_id": "..." }
```

Inbound (Node → Python):

```
{ "type": "response", "request_id": "<uuid>", ...payload }
{ "type": "error",    "request_id": "<uuid>", "error": "..." }
{ "type": "event",    "request_id": null, "session_id": "...", "event": "<kind>", "data": {...} }
```

Demux rule on the Python side:

- A message with a `request_id` resolves the matching pending future.
- A message without a `request_id` is a build event and is routed to a
  per-session asyncio queue by `session_id`.

This separation is the reason concurrent builds cannot race for the same
stdout — responses and events live on disjoint routing paths.

### Discord voice sidecar (`discord_voice_bridge/index.js`)

Single-direction request/event protocol; no `request_id` needed because there
is at most one voice channel in flight.

Outbound (Python → Node):

```
{ "action": "join",     "channel_id": "..." }
{ "action": "leave" }
{ "action": "play",     "pcm_b64": "<base64 PCM>" }
{ "action": "shutdown" }
```

Inbound (Node → Python):

```
{ "event": "ready" }
{ "event": "joined",  "channel_id": "..." }
{ "event": "left" }
{ "event": "audio",   "pcm_b64": "<base64 PCM>", "user_id": "..." }
{ "event": "error",   "message": "..." }
```

The sidecar **filters at the Node side**: only audio frames from the
authorized voice user ID are forwarded. Other speakers are dropped before
they reach Python.

### MCP servers

The MCP Python SDK is the client; servers speak the protocol on their own
terms (each is a third-party process). Tool discovery happens at startup;
tool dispatch is per-call; auditing happens around every call.

---

## Audio Direction Conventions

```
                 Discord                Python                 Gemini Live
                                                              (or Grok)
  speaker -->  48 kHz stereo  -->  (Node downsamples)  -->   16 kHz mono PCM s16le
                                                              (model input)

  user  <--   48 kHz stereo   <--  (Node upsamples)    <--   24 kHz mono PCM s16le
                                                              (model output)
```

These two rates are the rates the model expects and produces. Python does no
audio processing of its own: the sidecar downsamples on receive and upsamples
on send, both via prism-media (FFmpeg under the hood). The PCM bytes that
cross the Python boundary are exactly what the model wants on each side.

The local-voice entry point (`src/local_voice.py`) uses the same rates and
talks to the OS audio devices directly via sounddevice. Format conventions are
identical so the rest of the system does not care which transport is in use.

---

## Callback Wiring

Cross-module behavior is wired by callback injection at startup, not by
imports. This keeps modules decoupled and testable.

### Tool handler injection

`src/bot.py::on_ready` calls `tools.init_tools(...)` once, passing in a set of
callbacks that tools may invoke during dispatch:

- `post_callback(text, thread=None)` — post to the text channel (or a specific
  thread). Used by `plan_with_claude`, `do_with_claude`, prompt show/edit.
- `alert_callback(text)` — post to the alerts channel. Used for confirmations,
  errors, cost notices, voice exits.
- `thread_callback(session_id, project) -> Thread` — create a Discord build
  thread for a Cursor session. Used by `build_with_cursor`.
- `cursor_event_callback(session_id, thread)` — read build events for a
  session and route them to the thread and into Gemini's context.
- `reconnect_callback()` — close and reopen the Gemini session, used by the
  prompt reload tool when Aria's own system prompt changes.

Tools never import from `bot.py`. They use whichever callbacks were injected.

### Voice audio callback

The voice transport invokes a registered audio callback for each inbound PCM
frame. `bot.py` registers a single function (`_on_voice_audio`) that knows
which conversational session is active (Gemini by default, Grok when the
SpicyLit channel is active) and forwards accordingly. The same function is
responsible for transparent resume from a paused session.

### MCP confirm callback

The MCP client owns the dispatcher; the bot owns the user. The two are wired
by `mcp.set_confirm_callback(...)` at boot. When a tier-I or tier-X tool is
about to fire, the dispatcher invokes the callback with an action ID,
the tool name, and a redacted summary. The callback:

- Posts the confirmation to the alerts channel.
- Asks the user out loud through Gemini.
- Awaits the user's spoken answer via `GeminiSession.wait_for_confirmation`.

The spoken answer arrives back as a Gemini function call (`confirm_action`),
which resolves the awaited event and unblocks the dispatcher with the user's
decision.

---

## Configuration & Secrets

Configuration is loaded once at import time. `src/config.py` reads `.env` via
`python-dotenv` into a frozen `Config` dataclass; nothing else imports
environment variables directly. Children inherit the env via subprocess
spawning.

`.env` holds Discord tokens (both bots), Discord identifiers (guild, channel
IDs, authorized user IDs), AI provider keys (Gemini, Anthropic, Cursor,
Grok), MCP-relevant secrets (GitHub, Google OAuth paths), cost guardrail
overrides, and capability-specific channel IDs.

`.env.example` is the canonical list of recognized variables. `.env` itself
is gitignored.

---

## Boot Lifecycle

`bot.py` runs the following sequence; the order is load-bearing.

1. **Module import** — `config.py` reads `.env` and freezes `Config`.
2. **`bot.run(token)`** — py-cord logs in and emits `on_ready`.
3. **`init_db()`** — SQLite schema applied (idempotent).
4. **`init_memory()`** — mem0 client constructed with Anthropic LLM and
   Gemini embedder, vector store path under `data/mem0/`.
5. **`tools.init_tools(...)`** — Anthropic client constructed, all callbacks
   injected, project registry loaded from `projects/registry.md`.
6. **`cursor_bridge.start()`** — Node Cursor wrapper spawned, reader and
   stderr-drain tasks started.
7. **`voice_bridge.start()`** — Node discord.js sidecar spawned, blocks until
   the sidecar emits `ready`.
8. **Gemini session constructed but not connected** — connection happens on
   join. The session is created here so tool callbacks have a stable
   reference.
9. **MCP fleet starts** — every server in the catalog is brought up; tools
   are collected; the confirm callback is wired.
10. **Preflight runs** — every advertised capability is probed end-to-end.
    The report posts to the alerts channel.
11. **Gate** — if any *critical* probe failed, `bot.py` returns without
    enabling `!join`. The text/commands surface still works so the user can
    investigate.
12. **Voice catch-up** — if a deferred join was queued during preflight, or
    if the authorized user is already in voice, `_auto_join_voice_channel`
    fires.

---

## Voice Session Lifecycle

The authorized user is the only voice the bot listens to.

### Join

```
on_voice_state_update (or on_ready catch-up)
  │
  ▼  (only for authorized user)
_auto_join_voice_channel(channel)
  │
  ├─ voice_bridge.register_audio_callback(_on_voice_audio)
  ├─ voice_bridge.join(channel_id)              ─→ Node joins WebSocket
  ├─ choose pipeline:
  │    ├─ SpicyLit channel    → start Grok session, drain its audio
  │    └─ otherwise            → connect Gemini, drain its audio
  └─ start watchdogs (idle pause + total-silence exit)
```

### Audio in

```
Discord (user speaks)
  → discord.js sidecar (filters by user_id, downsamples)
  → Python: _on_voice_audio(pcm)
      ├─ if paused: reconnect session, replay short transcript context
      └─ forward PCM to active session (Gemini or Grok)
```

### Audio out

```
Active session emits PCM (24 kHz mono)
  → drain task (one per active session)
  → voice_bridge.send_audio(pcm)
  → discord.js sidecar (upsamples)
  → Discord
```

### Pause

After a short idle window, the per-session watchdog:

- Captures the recent transcript context.
- Closes the model WebSocket.
- Sets a paused flag and keeps the Discord voice connection open.

The next inbound user utterance (`_on_voice_audio`) sees the flag, reconnects
the session, replays the saved context as `turn_complete=False` background,
and re-arms the watchdogs.

### Exit

A separate, longer-window watchdog fires on total silence regardless of the
pause state, leaves the voice channel, closes any active session, and posts
to the alerts channel. The authorized user leaving voice triggers the same
cleanup immediately.

---

## Tool Call Lifecycle

```
Gemini decides to call a tool
  │
  ▼
GeminiSession._receive_loop dispatches a function_call
  │
  ├─ confirm_action: handled inside the session (resolves a pending await)
  └─ everything else: tool_handler(name, args)
                       │
                       ▼
                tools.handle_tool_call(name, args)
                       │
                       ├─ spend ceiling check
                       ├─ handler lookup (flat dict)
                       ├─ run handler (single shot, agent loop, or Cursor delegate)
                       ├─ log_event(name, args, result, duration, cost)
                       └─ return result JSON string
                       │
                       ▼
GeminiSession sends a function_response back
                       │
                       ▼
Gemini speaks the user-facing summary
```

The tool dispatcher is the single chokepoint for cost tracking and per-tool
auditing of the local catalog. The MCP dispatcher is the analogous
chokepoint for MCP-backed actions and writes to `data/audit.jsonl`. It is also
where universal verified-done lives: after a state-changing (W/I/X) verb
returns, `src/anchors/postcondition.py` re-consults the source of truth — a
provably-absent artifact becomes a typed `unverified` error (BLOCKED by
`src/outcomes.py`), an unconfirmable one a loud annotation — so a write is
verified where and when it fires, not minutes later in the async judge.

---

## Cursor Build Lifecycle

```
build_with_cursor(project, instruction)
  │
  ├─ resolve project → absolute path (from projects/registry.md)
  ├─ prepend prompts/implementation.md
  ├─ cursor_bridge.create_session(...)              ─→ Node spawns an Agent
  ├─ upsert cursor_sessions row (status=running)
  ├─ create Discord build thread
  └─ spawn _cursor_event_consumer(session_id, thread)
                                  │
                                  ▼
        async iterates events from the per-session queue:
          file_edit / test_run   → throttled summary to thread
          question               → forwarded to Gemini for spoken Q
          completion             → thread message + Gemini narration + db update
          error                  → alerts post + db update
```

Multiple builds can run concurrently. Their events live on disjoint queues
keyed by `session_id`; the Cursor bridge's single stdout reader is the only
component that touches the shared stream.

---

## State Wiring

Each store is touched by a small, named set of writers.

### SQLite (`data/state.db`)

| Table | Writers | Read by |
|---|---|---|
| `cursor_sessions` | `tools._build_with_cursor`, `bot._cursor_event_consumer` | `tools._cursor_status`, status command |
| `events` | `tools.handle_tool_call`, Claude call sites, `bot._log_grok_cost`, `preflight.probe_db` | `db.get_daily_spend`, status command |
| `planning_history` | `tools._plan_with_claude` | `tools._plan_with_claude` |
| `discord_threads` | (reserved for future thread<>session bindings) | — |
| `spicylit_stories` | `capabilities/spicy_lit/db.save_outline` | `capabilities/spicy_lit/db.get_latest_outline` |

### mem0 (`data/mem0/`)

Writers: `tools._remember`, deep-integration tests, preflight sentinel.
Readers: `tools._recall`, planning context assembly, agent-loop context assembly.

### Audit (`data/audit.jsonl`)

Writer: `mcp._audit_log` (called inside `mcp.MCPClient.call_tool` on every
attempt, including declines).
Reader: humans. Nothing in the bot reads this file.

---

## Failure Modes

How each subsystem fails, and what the system does about it.

| Subsystem | Failure | Behavior |
|---|---|---|
| Gemini Live WebSocket | Drops mid-session | Receive loop catches the exception, exponential backoff, reconnect, replay short transcript as background context. |
| Anthropic API | Network or model error | Tool returns a JSON `error`; Gemini speaks the error; the user can retry. |
| Cursor wrapper | Subprocess dies | All pending futures resolve with `CursorBridgeError`; event consumers drain. Bridge is not auto-restarted; restarting the parent (`make run` or `deploy.sh`) cycles it. |
| Discord voice sidecar | Subprocess exits | Reader logs the exit; `!join` will fail noisily until the parent is restarted. |
| MCP server | Server crashes | The catalog entry is still present but calls return an error; `health_check` shows it as down; preflight on rerun catches it. |
| Stale launch / branch drift | Running process is not the pinned trunk, the build tree is dirty, or source changed since boot | CRITICAL `deployed_trunk` probe refuses ready and prints the exact fix (`git checkout main … && make run`). Every launch path goes through `ops/launch.sh`, which checks out `main` first, so a restart returns to the trunk (it can never re-bless a feature branch the way the old WARN `running_code` did). |
| Dependency drift | numpy>=2, discord.py instead of py-cord, etc. | `dep_drift` probe fails critical with the fix command. |
| Cost ceiling | Daily cap reached | Dispatcher returns an error for paid tools; read-only and control tools still work. |

---

## Shutdown

`bot.run(...)` returns when py-cord exits. The bot does not currently have a
graceful shutdown path on SIGTERM; `kill.sh` (or launchd) terminates the
process tree and the OS reaps the sidecars. The next launch starts cleanly
because:

- SQLite is WAL with `busy_timeout`.
- mem0 stores files atomically.
- The audit log is append-only.
- `ops/launch.sh` checks out `main` and `src/build_hash.stamp_boot()` freezes the
  build hash at boot; the CRITICAL `deployed_trunk` probe compares the live build
  to it (and to the trunk) so a drifted or post-boot-edited process refuses ready.

If you add a graceful shutdown, the sequence to follow is the reverse of
boot: close Gemini → stop MCP fleet → stop voice bridge → stop cursor bridge →
return from `on_ready`.
