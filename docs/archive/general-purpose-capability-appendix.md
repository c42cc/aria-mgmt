# Appendix: General Purpose Capability

**Companion to:** `voice-plan-build-unified-plan.md`. The main plan defined the Voice → Plan → Build loop. This appendix extends it to general-purpose assistant capability — email, files, calendar, browser, messages, notes, system control — using MCP as the unified extension layer.

This document is conceptual. Engineers decompose into concrete implementation steps from here.

---

## What This Adds

The base plan gives you a voice-controlled software engineering loop. This appendix turns the same system into a general-purpose personal assistant that can:

- Read and triage your email; draft replies; send messages.
- Read and modify your calendar; find conflicts; schedule meetings.
- Search, read, move, write, and organize files anywhere on your Mac.
- Browse the web; fill forms; automate browser tasks.
- Read and send iMessages; query Notes, Reminders, Contacts.
- Send and read Slack messages; search Slack history.
- Run shell commands (gated by risk tier).
- Take complex multi-step actions ("read my unread mail, summarize the urgent ones, draft replies to the three from clients, then book a 30-minute window with Mike on Thursday").
- Run scheduled routines proactively.

Everything is additive. The plan-and-build loop continues to work unchanged. The Cursor tool stays as the specialized path for code. The new capability layers in beside it.

---

## What Would Need to Be True (Restated as Requirements)

Each of these is implemented in a section below.

1. The agent must access your digital life through a standard protocol, not bespoke integrations per service. → **MCP (Model Context Protocol).**

2. Complex multi-step tasks must route to a strong reasoner with tool access, not be force-fit into Gemini's function-calling loop. → **`do_with_claude` tool: Claude Opus 4.6 with MCP tools attached.**

3. Irreversible and high-impact actions must require confirmation, and the system must be auditable. → **Tool risk tiers + confirmation flow + append-only audit log.**

4. Persistent context must span diverse domains, not just the current project. → **mem0 with categorized memory namespaces.**

5. Multi-step routines must be definable, repeatable, and optionally scheduled. → **Workflow files + cron-like scheduler.**

6. The tool catalog must be navigable when it grows past a few dozen tools. → **Tool subsetting and search-based discovery.**

7. MCP servers must run reliably as managed processes. → **launchd-supervised server fleet with health checks.**

---

## Architectural Shift

The main plan's three intelligences become three roles with sharper definition:

**Gemini 3.1 Live — the conversational layer and fast-path dispatcher.** Unchanged in role. Gains a wider tool surface for common one-shot operations ("read my next calendar event," "list files in ~/Downloads"). Anything requiring reasoning or multi-step action gets delegated.

**Claude Opus 4.6 — the general reasoner and agent.** Substantially expanded. Was previously a planner (text in, text out). Now:
- `plan_with_claude` — pure planning, no side effects. (Unchanged from main plan.)
- `do_with_claude` — Claude with MCP tool access. Reasons, acts, observes, acts again. Returns a summary.

**Cursor SDK — the specialized code builder.** Unchanged. Stays as the engineering path. Not used for general tasks.

The key insight: **Claude Opus 4.6 is the only model in the system capable of multi-step reasoning over a non-trivial tool set.** Gemini is fast but shallow; Cursor is deep but scoped to code. For everything else — email triage, research, file organization, complex personal tasks — Claude is the doer, not just the planner.

---

## MCP: The Extension Protocol

Model Context Protocol is a standard that defines how LLM clients talk to tool-providing servers. Each MCP server is a standalone process that:

- Advertises its tools (with JSON schemas).
- Accepts tool invocations.
- Returns structured results.
- Communicates over stdio, HTTP, or WebSocket.

There are hundreds of community and official servers. We use the protocol, not a framework on top of it. Anthropic publishes a Python SDK (`mcp`) and a TypeScript SDK; both are thin clients.

### Why MCP, not custom integrations

Building per-service integrations would mean: writing a Gmail client, an iMessage bridge via AppleScript, a Notion API wrapper, a filesystem tool, a browser automation harness. Each with its own auth, its own error handling, its own update treadmill. MCP gives you the same capability through one client interface, and the servers are maintained by their authors.

When a service breaks, you update the server. When a new service is needed, you install another server. No code changes in the bot.

### Where MCP lives in the architecture

```
   ┌─────────────────────────────────────┐
   │  bot.py                             │
   │                                     │
   │  Gemini Live session                │
   │   ├─ direct Tier 1 tools            │◄────┐
   │   │                                 │     │
   │   └─ delegate_to_claude(task)       │     │
   │         │                           │     │
   │         ▼                           │     │
   │  Claude Opus 4.6 session             │     │ all MCP tools
   │   └─ with MCP tool access ──────────┼─────┤
   │                                     │     │
   │  MCP client (Anthropic SDK)         │     │
   │   ├─ tool catalog                   │     │
   │   ├─ tool search                    │     │
   │   └─ dispatch                       │     │
   └──────┬──────────────────────────────┘     │
          │                                    │
   ┌──────▼────────────────────────────────────┼─────────────────┐
   │  Local MCP server fleet (launchd-supervised)               │
   │                                                            │
   │  apple-mail        filesystem      shell         git       │
   │  apple-calendar    web-browser     memory        ...       │
   │  apple-messages    google-drive                            │
   │  apple-notes       slack                                   │
   └────────────────────────────────────────────────────────────┘
```

The MCP client is initialized once at bot startup, connects to all configured servers, builds the tool catalog. Claude sessions (via `do_with_claude`) get tool access scoped per request.

---

## The New Tool Surface

### Gemini's tools (Tier 1 — always available)

These are what Gemini sees in its function declarations. Stays small (12-15 tools) so Gemini's context isn't bloated:

| Tool | Use |
|---|---|
| `plan_with_claude` | Pure planning, no side effects. (From main plan.) |
| `do_with_claude` | Complex tasks needing reasoning + actions. The general-purpose hammer. |
| `build_with_cursor` | Code building. (From main plan.) |
| `query_cursor` | Talk to a running Cursor session. (From main plan.) |
| `cursor_status` | List active Cursor sessions. (From main plan.) |
| `read_file` | Read a file by path. Pre-baked from filesystem MCP for low latency. |
| `list_files` | List files in a directory. Pre-baked. |
| `quick_calendar` | Today's and tomorrow's events. Pre-baked from calendar MCP. |
| `quick_email_check` | Count of unread, top 5 senders. Pre-baked from mail MCP. |
| `quick_message` | Send a quick iMessage or Slack DM. Pre-baked. |
| `remember` | Store a fact in long-term memory. |
| `recall` | Search long-term memory. |
| `run_workflow` | Invoke a workflow by name. |

The pattern: tools used for fast, single-shot user requests are pre-baked as direct Gemini functions; tools used in multi-step tasks are accessed by Claude via the full MCP catalog.

### Claude's tools (Tier 2 — full MCP catalog, accessed via `do_with_claude`)

When `do_with_claude` is invoked, Claude is given access to the MCP tool catalog. Two approaches:

**Approach A — full catalog (small fleets):** Every MCP tool from every connected server is in Claude's tool list. Works up to ~30 tools.

**Approach B — tool search (large fleets):** Claude sees a meta-tool `search_tools(query)` that returns the top N relevant tools for the task description. Claude then sees the actual tools for that domain. Necessary once tool count crosses ~50.

Start with Approach A. Migrate to Approach B when the catalog grows.

### The `do_with_claude` tool spec

```
Parameters:
  task: string             — Natural language description of what to do
  domain_hint: string?     — Optional: "email" | "files" | "calendar" | etc.
                             Restricts tool catalog to that domain.
  allow_writes: boolean    — Default true. If false, Claude can only read.
                             Used for dry-run mode.
  session_key: string      — Discord thread ID

Returns:
  summary: string          — Natural language description of what was done
  actions: list[Action]    — Structured log of each tool call
  needs_confirmation: list — Any pending confirmations awaited

Behavior:
  1. Build tool list (filtered by domain_hint if present, or via search).
  2. Open Claude Opus 4.6 session with system prompt for agentic work.
  3. Pass task as user message.
  4. Loop: Claude responds with text or tool calls.
     - For tool calls: route through MCP client, return results to Claude.
     - For confirmation-required tools: pause, surface to Gemini, get user
       answer via voice, then proceed.
     - For text responses: collect.
  5. Return final summary and structured action log.
```

The agent loop inside `do_with_claude` is conventional: a while-loop that alternates Claude calls and tool invocations until Claude stops calling tools.

---

## Recommended Starter MCP Server Set

Install in this order. Each is independently useful.

### Apple ecosystem (zero-OAuth, AppleScript-based)

These work via AppleScript / JXA and require no third-party auth. They are the fastest path to "the agent can do useful things on my Mac." Community servers under names like `mcp-server-apple-mail`, `mcp-server-apple-calendar`, etc.

- **apple-mail** — Read inbox, search, read message bodies, send mail, draft replies, archive, mark read.
- **apple-calendar** — Read events for ranges, create events, find free slots, delete events.
- **apple-notes** — List, read, create, append to Notes.
- **apple-messages** — Read recent iMessages, send messages to contacts.
- **apple-reminders** — List, create, complete reminders.
- **apple-contacts** — Search contacts, get phone/email.

If you're a Gmail/Google Calendar user instead, replace `apple-mail` and `apple-calendar` with `google-mail` and `google-calendar` (OAuth required, see auth section).

### Filesystem and local

- **filesystem** — Read/write/move/delete files within configured allowed directories. The official `@modelcontextprotocol/server-filesystem` from Anthropic.
- **shell** — Execute shell commands. **High risk tier.** Required for many automation tasks (git, system queries, scripts). Gated by confirmation by default.
- **memory** — Wraps mem0 as MCP. Allows Claude to remember and recall without going through Gemini.

### Web and external

- **web-browser** — Playwright/Puppeteer-based browser automation. Open pages, click, fill forms, extract content. For "go log into the bank and download my statement" type tasks.
- **fetch** — Simple URL fetching for non-interactive web reads.
- **web-search** — Search the web (uses Brave/Google/DuckDuckGo API). For research tasks.

### Work-context

- **slack** — Read channels, send DMs, search history. OAuth required.
- **google-drive** or **dropbox** — If you use cloud storage.
- **notion** or **obsidian** — Notes-app integration if you use one of these instead of Apple Notes.
- **git** — Direct git operations (status, log, diff, blame). Useful even with Cursor.

### Optional later

- **github** — Issues, PRs, comments. OAuth.
- **linear** — Project management.
- **stripe**, **figma**, etc. — Domain-specific tools.

Start with the Apple set + filesystem + shell + web. That covers 80% of "general personal assistant" tasks without any OAuth ceremony.

---

## Tool Risk Tiers and Safety

Every MCP tool gets categorized at registration time. The bot enforces behavior by tier.

### Tier definitions

**Tier R — Read-only.** Safe. No confirmation. No special logging beyond standard.
*Examples:* `read_file`, `list_files`, `search_email`, `get_calendar_events`, `read_message`, `search_web`.

**Tier W — Reversible writes.** Configurable confirmation, default on for first 30 days then off. Logged with full args.
*Examples:* `create_draft_email`, `create_calendar_event`, `write_file_new`, `create_note`, `set_reminder`.

**Tier I — Irreversible writes.** Always require confirmation. Logged with full args and confirmation text.
*Examples:* `send_email`, `delete_file`, `delete_calendar_event`, `git_push`, `move_file_outside_workspace`.

**Tier X — Dangerous / arbitrary execution.** Always confirm. Always require dry-run preview where possible. Always logged. Restricted by allowlist (you opt-in per command pattern).
*Examples:* `shell_execute`, `sudo`-prefixed commands, `rm -rf`, anything modifying `~/.ssh`, `~/.aws`, `~/Library/Keychains/`.

### Confirmation flow

When Claude proposes a Tier I or Tier X action:

1. The agent pauses the loop. Claude's proposed tool call is held.
2. Bot constructs a confirmation prompt: "About to send an email to john@example.com with subject 'Re: Q4 numbers.' Body preview: '[first 200 chars]...' Send it?"
3. Gemini speaks the prompt and posts it to the text channel.
4. User responds by voice or text: "yes" / "no" / "wait, change the subject to..."
5. On yes: tool executes.
6. On no: Claude is told the action was declined, continues without it.
7. On modification: Claude is told the modification, may re-propose.

For Tier W actions, the same flow runs only if confirmation is enabled (default: yes for the first 30 days of use, then off).

### Allowlist and denylist

A markdown file at `safety/policy.md` defines:

- **Allowlisted shell commands:** patterns (regex or prefix) that auto-approve without confirmation. Example: `git status`, `git log`, `ls`, `cat ~/Documents/*`. Edit this file to grant more.
- **Denylisted paths:** paths the filesystem and shell tools refuse to touch entirely. Example: `~/.ssh`, `~/.aws`, `~/.config/gh`, `/System`, `/usr/bin`. Modify only with extreme intention.
- **Domain-restricted tools:** which tools may be invoked from which domain contexts. Example: `git_push` only allowed when invoked inside `build_with_cursor`, never from `do_with_claude` directly.

### Audit log

`data/audit.jsonl` — append-only, one line per tool invocation regardless of tier. Includes:

- Timestamp
- Tool name (with MCP server namespace)
- Full arguments (redacted by tool's redaction policy — e.g., passwords masked)
- Result summary or error
- Confirmation status if applicable
- Session context (Discord thread, user)

This is separate from the regular `events.jsonl` and is meant to be tamper-evident (append-only via `O_APPEND` on the OS level). It's what you read when you ask "what did the bot do last Tuesday."

### Kill switch

`!stop` in Discord text immediately aborts any running `do_with_claude` or `build_with_cursor` session. The Discord listener checks for this command on a separate task and signals the session loop to terminate.

Physical kill switch: `launchctl bootout gui/$(id -u)/com.you.voicebot` stops the bot entirely. Have this aliased to a keyboard shortcut.

---

## Authentication Setup

The Apple ecosystem MCP servers need no auth — they run as the user. Other servers need OAuth or API keys.

### OAuth flow (Gmail, Calendar, Slack, Drive, etc.)

Each OAuth-based MCP server has a one-time setup ceremony:

1. Install the server.
2. Run the server's auth command, which opens a browser to the service's OAuth consent page.
3. Authorize. The server caches refresh tokens locally (usually in `~/.config/<server-name>/`).
4. From then on, the server refreshes its tokens automatically.

Tokens are sensitive. They go in OS-level secure storage when the server supports it (macOS Keychain via `security`), otherwise file-permissioned to user-only.

Centralize this: a `setup/auth.sh` script runs each server's auth command in order and verifies each one returned a token. After running once, all servers are authed.

### API keys

For servers using API keys instead of OAuth (Brave Search, etc.), keys go in `.env` alongside the existing four. Pattern: `MCP_<SERVER>_API_KEY=`.

### Revocation

`safety/revoke.sh` — a script that revokes all stored OAuth tokens and clears API keys. For when you suspect compromise or are decommissioning. Document this in the README.

---

## Memory Across Domains

The main plan uses mem0 for software-task context. For general-purpose use, expand to multiple namespaces.

### Namespaces

- `user/profile` — Durable personal facts. Name, family, allergies, preferences, location. Curated.
- `user/contacts` — People you talk about: "Mike is my CTO, his email is mike@...". Curated.
- `projects/` — One namespace per project. Same as main plan.
- `domains/email` — Patterns the agent has learned: "user always replies to client emails within 4 hours," "user uses British English in formal emails."
- `domains/calendar` — "User doesn't book meetings before 10am," "Wednesdays are deep-work days."
- `episodes/` — Recent task summaries. Time-bounded. Pruned automatically.

mem0 supports user_id-style scoping; we use it for namespacing.

### Writing to memory

- **Explicit writes** by user request: "remember that my mom's birthday is March 14." Routes to `user/profile`.
- **Inferred writes** by Claude after task completion: at the end of `do_with_claude`, Claude is prompted "is there anything from this task worth remembering for future use?" If yes, it writes to the appropriate namespace.
- **Episode writes** automatic: every task completion writes a short episode summary to `episodes/` with timestamp.

### Reading from memory

- Before any `do_with_claude` call, the bot pulls relevant memories from `user/profile`, `user/contacts`, and the inferred-relevant `domains/*` namespace, injects them as context.
- For ad-hoc recall by the user ("did we talk about the Q4 strategy?"), Gemini calls `recall` directly.

### Forgetting

`mem0 has delete operations. Expose a `forget` action so the user can say "forget that I told you X" and the agent removes the relevant memories. Important for incorrect facts and for privacy.

---

## Workflows Beyond Software

Workflows are markdown files in `workflows/`. Each describes a multi-step task in natural language with placeholders. They're called by name via `run_workflow(name, args)`.

### Format

```markdown
---
name: morning-briefing
description: Daily summary at start of work day.
schedule: "0 8 * * 1-5"    # optional cron expression
tools_hint: ["mail", "calendar", "fetch"]
---

# Morning Briefing

Run these steps:

1. Check today's calendar via the calendar MCP. Note any meetings.
2. Check the unread email count since yesterday 5pm. Identify any from
   the list of priority senders in user/contacts memory.
3. Fetch today's weather for the user's city (from user/profile memory).
4. Compose a 90-second spoken briefing:
   - Top priority emails (sender + one-line summary).
   - Calendar overview (meetings with time + people).
   - Weather and any commute-relevant notes.

Speak the briefing. Post the detailed version to the text channel.

If any of the priority emails seem urgent, ask the user if they want
to start triaging.
```

### Examples to include

Ship the system with a starter set:

- `morning-briefing.md` — described above.
- `evening-wrap.md` — end-of-day summary, unsent drafts, tomorrow's calendar.
- `triage-inbox.md` — read unread, categorize, draft replies to top three.
- `meeting-prep.md` — invoked with `args.event_id`, pulls calendar event, finds related email threads, summarizes context.
- `weekly-review.md` — Fridays, summarize the week's completed work, open threads, things slipped.
- `plan-and-build.md` — the original software workflow, now one of many.

Users author more by saying "save this as a workflow called X" — Gemini writes the file based on the conversation it just had.

---

## Scheduled and Proactive Tasks

The scheduler is a tiny Python module that reads workflows with `schedule:` front matter and dispatches them via the same `run_workflow` path.

### Mechanism

- On bot startup, scan `workflows/*.md`. Collect any with a `schedule:` field.
- Spawn an `apscheduler` (or `schedule` library) task per workflow.
- At trigger time, call `run_workflow(name)`. Output posts to a configured channel (default: `#bot-text`) and, if you're in voice channel, speaks the summary.

### Proactive notifications

Beyond scheduled tasks, the bot can be configured to surface events as they happen:

- "Email from a priority sender arrived" → post to text + speak if in voice.
- "Calendar event starting in 5 minutes" → reminder.
- "Cursor build that's been running for 20 minutes has stalled."

Each is a small watcher (a background asyncio task) that polls or subscribes via the relevant MCP server, checks against rules in `workflows/notifications.md`, and posts accordingly.

### Rate limiting and quiet hours

`workflows/notifications.md` includes quiet-hours config (e.g., no proactive speech 10pm–7am) and a max-notifications-per-hour cap. The bot enforces these globally.

---

## Tool Subsetting and Context Management

Once you connect 8–10 MCP servers, the tool count climbs. Two-stage discovery keeps Claude's context tight.

### Strategy

When `do_with_claude` is called:

1. **Stage 1 — domain inference.** If `domain_hint` was supplied, use it. Otherwise, do a cheap inference call (use Haiku 4.5 for this — it's a routing decision, not the main work): "Given this task, which of these domains apply? [list of domain names]." Returns one or two domains.

2. **Stage 2 — tool subsetting.** Load only the tools belonging to those domains, plus a small set of always-available tools (memory, filesystem read). Pass this subset to Claude Opus 4.6 as the tool list.

3. **Stage 3 — escalation.** If Claude requests a tool not in the subset, it can call `search_tools(query)` to find additional tools. Result is presented; Claude may invoke them, dynamically expanding its tool list mid-task.

### Domains

Tools are tagged at registration with one or more domains:

```yaml
# In mcp_servers.yaml
- name: apple-mail
  command: "uvx mcp-server-apple-mail"
  domains: ["email", "communication"]
  tier_defaults:
    read_*: R
    send_*: I
    draft_*: W
```

The tier_defaults block per-server is critical — it's how risk tiers get assigned without editing each tool individually.

---

## MCP Server Process Management

MCP servers are processes. They need supervision.

### Configuration file

`mcp_servers.yaml` lists every connected server:

```yaml
servers:
  - name: apple-mail
    transport: stdio
    command: "uvx mcp-server-apple-mail"
    domains: ["email"]
    tier_defaults: { read_*: R, send_*: I, draft_*: W }
    
  - name: filesystem
    transport: stdio
    command: "npx @modelcontextprotocol/server-filesystem"
    args: ["/Users/you/Documents", "/Users/you/Downloads", "/Users/you/code"]
    domains: ["files"]
    tier_defaults: { read_*: R, write_*: W, delete_*: I, move_*: W }
    
  - name: shell
    transport: stdio
    command: "uvx mcp-server-shell"
    domains: ["system"]
    tier_defaults: { execute: X }
    allowlist_file: "safety/shell_allowlist.txt"
```

### Lifecycle

On bot startup, the MCP supervisor module:

1. Reads `mcp_servers.yaml`.
2. Spawns each server as a subprocess with the configured transport.
3. Performs an `initialize` handshake to verify it's alive and get its tool list.
4. Registers tools in the central catalog with the configured tier defaults.
5. Sets up a health check (`ping` every 60 seconds; restart if no response).

On bot shutdown, all server subprocesses are terminated gracefully.

### Failure handling

If a server dies mid-task: the in-flight tool call returns an error to Claude with the message "server X temporarily unavailable." The supervisor restarts the server. Claude can retry or proceed without that tool.

### Logs

Each MCP server's stdout/stderr is redirected to `~/Library/Logs/voicebot/mcp/<server-name>.log` for diagnostic purposes. Rotated daily, kept 14 days.

---

## Build Phases (Additions to the Main Plan)

These extend the main plan's phases. Run them after the main Phase 3 gate is hit.

### Phase 5 — MCP foundation (Week 4)

1. Add MCP client (Anthropic Python `mcp` SDK).
2. Add `mcp_servers.yaml` and the supervisor module.
3. Install and connect the Apple set: mail, calendar, notes, messages, reminders, contacts.
4. Add filesystem MCP server with allowlisted directories.
5. Add shell MCP server with a tight initial allowlist (only `git status`, `ls`, `cat`, `pwd`).
6. Implement `do_with_claude` tool: Claude session with MCP tools attached, basic agent loop.
7. Implement Tier 1 pre-baked tools (`read_file`, `list_files`, `quick_calendar`, `quick_email_check`).

**Gate:** Speak "what's in my inbox" — get a count and top senders. Speak "summarize the email from Mike that arrived this morning" — get a summary. Speak "create a draft reply saying I'll get back to him tomorrow" — confirmation surfaces, you approve, draft is created (not sent).

### Phase 6 — Safety and audit (Week 4-5)

1. Implement tool risk tiers in the dispatcher.
2. Implement confirmation flow through Gemini.
3. Implement audit log (`data/audit.jsonl`).
4. Implement `!stop` kill switch.
5. Define `safety/policy.md`, `safety/shell_allowlist.txt`, `safety/denylist.txt`.
6. Add a daily "summary of yesterday's actions" workflow.

**Gate:** Every Tier I action surfaces a confirmation prompt. Trying to delete a denylisted path returns a refusal. The audit log shows every action of the previous day.

### Phase 7 — Memory expansion (Week 5)

1. Expand mem0 namespaces as specified.
2. Implement `remember` / `recall` / `forget` Gemini tools.
3. Implement automatic episode writing at end of each `do_with_claude` call.
4. Implement memory injection at start of each `do_with_claude` call.
5. Run a memory-curation workflow that consolidates and prunes weekly.

**Gate:** Tell the agent three durable facts about yourself in one session. Start a new session a week later. The agent demonstrates it remembers them when relevant.

### Phase 8 — Workflows and scheduling (Week 5-6)

1. Implement workflow loader (parse front matter, load body).
2. Implement `run_workflow` tool.
3. Implement scheduler with `apscheduler`.
4. Author the starter workflow set (morning-briefing, evening-wrap, triage-inbox, meeting-prep, weekly-review).
5. Implement save-workflow flow (Gemini writes new workflow files from conversation).

**Gate:** Morning briefing fires at 8am, speaks the summary if you're in voice channel. You manually save a new workflow during the day ("save this as 'pre-flight'"). Tomorrow it runs at the scheduled time.

### Phase 9 — Wider tool fleet (Week 6+)

1. Add web-browser MCP server.
2. Add web-search MCP server.
3. Add Slack MCP if applicable.
4. Add Google Drive / Notion / Obsidian as applicable.
5. Migrate to tool subsetting / search-based discovery when catalog crosses ~30 tools.

**Gate:** Run a research task: "Find the latest pricing for the top three providers in [category], compare them in a doc, share the doc with me." The agent uses web-search, web-browser, filesystem, and possibly drive/notion. Returns a polished result.

### Phase 10 — Proactive observation (Week 6+)

This is the universal-constructor territory you originally wanted, repurposed.

1. The bot watches its own audit log via a heartbeat cron.
2. When it detects a sequence pattern (e.g., "every Monday morning the user opens these three files and copies into a doc"), it surfaces a proposal: "I noticed a pattern. Want me to save it as a workflow?"
3. On approval, it writes a workflow file.
4. The Curator (a separate scheduled task) periodically reviews workflow execution success rates and proposes deletions or revisions.

**Gate:** The bot proposes a workflow you'd actually use, unprompted. You promote it. It works.

---

## Engineer Checklist (Additions)

After the main plan's Phase 3 is green, add in this order:

1. MCP client integration (`mcp` Python SDK). Test against a single server (filesystem) end-to-end.
2. `mcp_servers.yaml` parser and supervisor module.
3. `do_with_claude` tool with agent loop. Test against filesystem-only first.
4. Apple ecosystem MCP servers installed and verified (one at a time).
5. Tier classification system + confirmation flow + audit log.
6. Memory expansion to multi-namespace.
7. Workflow loader and scheduler.
8. Web and external MCP servers.
9. Tool subsetting (when needed).
10. Proactive watcher (Phase 10, optional).

Code count target for this layer: ~800 additional lines of Python. Most of the work is configuration files, prompt engineering, and policy markdown.

---

## What's Still Not Built

Even with general-purpose capability, these stay out:

- **No multi-tenancy.** Single user, single Mac. If teammates need it, build a separate instance, don't refactor.
- **No remote agents on other machines.** Everything runs on your Mac. Remote actions happen via MCP servers that hit remote APIs.
- **No GUI / dashboard.** Discord is still the UI. Audit log inspection is via `tail` and a script. A web dashboard is a future project, not v1.
- **No LLM fine-tuning loop.** The audit log + episodes give you the data if you ever want it, but training is out of scope.
- **No agent-to-agent collaboration.** One Claude does the task. Subagents (Claude spawning Claudes) are tempting but add complexity for marginal gain at this stage.
- **No tool that the user hasn't explicitly enabled.** Every MCP server in `mcp_servers.yaml` was added intentionally. No auto-installing tools based on what Claude wants.

---

## Open Questions to Resolve

Flag these to the user before starting Phase 5.

1. **Email backend.** Apple Mail (zero auth, works immediately) or Gmail (OAuth ceremony, better search, works the same on phone)? Recommendation: Apple Mail if your mail account is configured in Mail.app on the Mac; otherwise Gmail.

2. **Calendar backend.** Same question. Apple Calendar (zero auth) or Google Calendar (OAuth). Pick the one your daily life uses.

3. **Notes backend.** Apple Notes, Obsidian, Notion, Bear, or something else? Determines which MCP server to install.

4. **Default confirmation behavior.** For Tier W (reversible writes), confirm by default for first 30 days then disable, or always confirm forever? Recommendation: 30-day training period.

5. **Quiet hours.** What hours should the bot not speak proactively? Default suggestion: 10pm–7am local.

6. **Priority senders.** Who's on your "always notify" list? This lives in `user/contacts` memory; seed it with five names + emails.

7. **Domain inference router.** Use Haiku 4.5 (~$0.001 per routing call) or skip it and always pass full tool catalog (simpler, more context per call). Recommendation: skip until catalog exceeds 30 tools.

8. **Browser MCP server choice.** Playwright-based (heavier, more capable) or simpler fetch-based (lighter, less capable). Recommendation: start with fetch + web-search; add Playwright only when you hit a "need to log into a site" task.

9. **Local model on the PC when the GPU arrives.** Worth using for routing decisions and cheap summarization? Recommendation: revisit after Phase 9. The cloud-only path will likely be fine; local model adds operational complexity for limited gain unless you have a specific cost or privacy reason.

---

## Why This Composition Works

The same loop you started with — Gemini conversing, Claude reasoning, tools acting — extends naturally to general-purpose capability because:

- **The conversational layer is unchanged.** Gemini is still the only voice. Adding 50 tools doesn't add 50 conversation styles; it adds 50 capabilities Gemini can dispatch.
- **The reasoner is unchanged in shape.** Claude Opus 4.6 was the planner. It becomes the planner-doer by getting tool access. Same model, same provider, same conversation pattern.
- **The builder stays specialized.** Cursor handles code. It doesn't expand. This keeps the engineering path tight.
- **MCP is the seam.** All new capability comes through one protocol. Adding an integration is configuration, not code.
- **The architectural non-negotiables hold.** No primary model, no second conversationalist, no agent framework, no orchestrator. Just glue, tools, and one strong reasoner.

The system is small enough to hold in your head. That property is preserved as it grows. The growth happens in MCP servers (which you don't write) and in workflow markdown (which you author by talking).

When you find yourself wanting to add a "router agent" or a "meta-orchestrator" or a "tool selection model" beyond domain inference, stop and ask whether you can solve it with a better prompt or a smaller tool subset instead. The answer is almost always yes.
