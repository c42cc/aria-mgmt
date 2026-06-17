# Aria — Vision

Aria is a voice-first personal operating shell. You talk to her from your phone. She talks back, thinks, acts on your computer, and reaches into the internet on your behalf.

One process on your Mac. No framework. Three intelligences with strict roles: one speaks, one reasons, one builds.

---

## What Aria Does

### General Computing

Aria is your interface to your own machine. Anything you can do from a terminal or a GUI, she can do by voice:

- File management — move, rename, search, organize, archive
- Shell commands — run scripts, check processes, manage services
- Application control — open, close, interact with Mac apps
- Clipboard and text manipulation
- System information and diagnostics

She does this through MCP servers that bridge her to the operating system. Adding a new integration means adding a new MCP server, not changing Aria's core.

### Internet

Aria handles the recurring internet tasks that eat your day:

- Email — read, draft, send, search, triage
- Calendar — check schedule, create events, resolve conflicts
- GitHub — issues, PRs, repo management, code review
- Web research — search, read, summarize, compare
- Any API-backed service reachable through an MCP server

Read-only questions ("any new mail?", "what's on my calendar?") get fast, direct answers. Actions that change state ("send that reply", "merge the PR") go through a confirmation step you hear out loud before anything fires.

### Software Development

The headline workflow. Describe a problem by voice. Aria gathers context, picks the right reasoning frame (planning, architecture, refactor, bug analysis), and produces a plan. You hear a summary, see the full plan in a text channel, and approve, adjust, or reject. On approval, she spins up a build on a fresh branch, streams progress, and delivers the diff.

Planning is iterative — each round sees the prior turns, so you refine by conversation, not repetition.

### Custom Loops

Aria supports self-contained capability packages that replace or extend her default behavior in specific contexts.

**SpicyLit.** A creative-writing loop. Entering its voice channel swaps the conversational layer to a storyteller persona. It collects your preferences by voice, generates a structured outline, persists it, and narrates the story. Pause, resume, and silence-exit all work the same as the default path.

**Universal Constructor.** A prompt-injection loop where Aria edits her own prompts. You tell her, by voice, to change how she behaves — her system prompt, her planning personas, her reasoning instructions. She applies the edit, posts the new version for your review, and reloads live. Behavior is data, stored as markdown files, not frozen in code. This is how Aria evolves without deploys. Every edit is versioned — you can ask Aria to list versions of any prompt and roll back to a previous one by voice. The UCS evaluation layer scores prompt versions against real usage data (did the plan get approved or revised?) and can recommend rollbacks, but user voice edits always win.

**UCS Integration (current state).** The UCS intelligence layer lives in `src/ucs.py` behind the `UCS_ENABLED` feature flag. When the flag is off (default), Aria uses the legacy direct-Anthropic code paths. When on, planning and agent tasks route through the UCS `IntelligenceLoop`, which reads model configuration from `models.yaml`, manages context budget via the `InjectionEngine`, and logs every execution with full observability (model_id, tokens, cost, truncation data) to the `loop_executions` table. The offline eval CLI (`src/eval.py`) reads this data to score prompt versions by approval rate. Both paths share the same MCP fleet, the same risk-tier confirmation mechanism, and the same cost guardrails.

New capabilities plug in by channel. You add a package, wire it to a Discord channel ID, and Aria routes audio to it when you enter that channel. The core loop doesn't change.

### Memory

Aria remembers what you tell her. Durable facts ("my CTO is Mike", "I'm allergic to peanuts", "the staging server is 10.0.1.42") persist across sessions and get pulled into future reasoning automatically. You don't re-explain context.

---

## Principles

**One process.** A single Python process owns everything. When it dies, launchd brings it back. No broker, no queue, no separate worker. The loop is readable end-to-end in one file.

**Strict role separation.** The conversational layer speaks and routes. The reasoning layer thinks and plans. The building layer writes code. None of them do each other's job. This is why the system stays small.

**Failures are loud.** No silent fallbacks. If a subsystem is down, preflight catches it before Aria enters ready state and tells you the exact command to fix it. If something breaks mid-session, you hear about it.

**Mechanical safety.** Risk tiers and cost guardrails are enforced at the dispatch layer, not by prompting the AI to be careful. Irreversible actions always confirm through you first. Daily spend ceilings and per-session limits are hard stops, not suggestions.

**Prompts are behavior.** Every persona, every instruction set, every reusable pattern is a markdown file read at runtime. Editing a prompt file changes how Aria acts on the next call. This is the primitive that makes the Universal Constructor loop possible.

**The Mac is the boundary.** Everything runs on your machine. Your code, your credentials, your data, your compute. There is no remote worker.

---

## Packaging for Others

Aria is designed to run on any Mac with the right dependencies installed. To set up a fresh instance:

1. Clone the repo
2. Run the bootstrap script (installs dependencies, builds sidecars)
3. Fill in `.env` with your own API keys and Discord application tokens
4. `make run`

Each user gets their own Discord server, their own bot identity, their own MCP server fleet, their own memory store. The codebase is shared; the configuration and state are personal.

What each user provides:

- A Google Cloud project (for Gemini API access)
- Anthropic API key (for Claude)
- Cursor subscription (for the build layer)
- Discord application tokens (two: one text, one voice)
- API keys for any optional MCP integrations

---

## Where This Goes

Aria's surface grows by adding MCP servers and capability packages, not by changing her core. The directions worth watching:

**More MCP integrations.** Each new server is a new verb — home automation, finance, music, messaging, databases, deployment pipelines. The pattern is always the same: a process that speaks JSON over stdio.

**More capability channels.** SpicyLit proves the pattern. Any domain with its own conversational style or LLM provider can be a channel: tutoring, brainstorming, interview prep, language practice.

**Scheduled actions.** Aria already runs under launchd. The natural extension is time-triggered tasks — morning briefings, end-of-day summaries, periodic checks — that run without a voice prompt.

**Local voice mode.** An alternative entry point that uses the Mac's microphone and speakers directly, no Discord required. Same tools, same prompts, same capabilities. Useful for when Discord is overkill or unavailable.

**Multi-modal input.** Screenshots, photos, documents passed through Discord or local capture, fed into the reasoning layer alongside voice context.
