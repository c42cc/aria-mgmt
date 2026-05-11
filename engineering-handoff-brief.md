# Engineering Handoff: Voice Agent System

**Read both attached documents before doing anything else:**
1. `voice-plan-build-unified-plan.md` — the core system
2. `general-purpose-capability-appendix.md` — extends it to general-purpose assistant

These two documents are the spec. Implement from them. Push back on anything that doesn't make sense, but don't change the architecture without my sign-off.

---

## What I'm Building and Why

A voice-controlled personal assistant I talk to from Discord on my phone. Three roles, strict separation:

- **Gemini 3.1 Live** is the conversational layer. The only voice I hear.
- **Claude Opus 4.6** is the reasoner. Does planning and complex multi-step tasks. Never speaks to me directly.
- **Cursor SDK** is the code builder. Never speaks to me directly.

Everything runs on my Mac as one Python process supervised by launchd. General-purpose capability comes through MCP servers (email, calendar, files, browser, etc.) — no bespoke integrations.

---

## Non-Negotiables — Do Not Change Without Asking

Violating these is what made the last attempt fail. They are the architecture.

1. **No fourth conversational layer.** No Claude Sonnet "primary model," no router agent, no orchestrator that talks to me. Gemini is the only voice in this system. Period.
2. **No agent framework.** No Hermes, LangChain, AutoGen, LangGraph. The loop lives in `bot.py` and is readable end to end.
3. **No memory system built from scratch.** Use mem0. Configure, don't extend.
4. **No GPU dependency.** Cloud APIs only for v1. The PC and the Nvidia GPU are out of scope.
5. **No PC handoff.** Everything on the Mac. The codebase is on the Mac, Cursor runs on the Mac, the bot runs on the Mac.
6. **MCP for all general-purpose integrations.** Do not write a custom Gmail client, a custom AppleScript wrapper, a custom Slack integration. Find the MCP server. If one doesn't exist, ask before writing one.
7. **Tool risk tiers are mechanical, not advisory.** Tier I (irreversible) always confirms. Tier X (shell, destructive) requires confirmation plus allowlist. Enforced by the dispatcher, not by prompting Claude to be careful.
8. **No model substitution.** Claude calls use Opus 4.6. Memory synthesis can use Haiku 4.5 (cheap, low-stakes). Never insert Sonnet anywhere.

If you think one of these is wrong, push back to me directly before implementing. Don't decide silently.

---

## What You Decide

Within the architecture, you own:

- File and module structure within the layout in the main plan.
- Concurrency model (asyncio is the obvious choice; defend if you pick otherwise).
- Logging library and structure.
- Test framework and coverage strategy.
- Error recovery details for the four failure modes (Gemini drop, Claude timeout, Cursor crash, Discord disconnect).
- Specific MCP server packages within the recommended set (community vs. official, Python vs. Node — pick what's maintained).
- launchd plist details.
- The Node ↔ Python protocol for the Cursor wrapper (JSON-line over stdio is fine; pick the dialect).

---

## What I Need to Provide You

Before you start coding:

- [ ] Mac admin access (or a user account with the right permissions).
- [ ] Anthropic API key.
- [ ] Google AI Studio / Vertex API key with Gemini Live access enabled.
- [ ] Cursor API key.
- [ ] Discord bot token + my Discord user ID + dedicated server with `#voice-bot`, `#bot-text`, `#bot-logs` channels.
- [ ] Confirmation on each of the open questions in the next section.
- [ ] A budget cap for monthly API spend (initial target: $200/month for development, will scale based on usage).
- [ ] Decision on which mail/calendar/notes apps I use (drives MCP server choices).
- [ ] A list of priority senders / contacts to seed memory with.

If any of these are missing on day one, you flag it on day one. Don't proceed without them.

---

## Open Questions I Need to Answer

I'll fill these in before kickoff. Paste my answers back into the relevant docs.

**From the main plan:**

1. mem0 backend LLM: [ Opus 4.6 / Haiku 4.5 — recommend Haiku 4.5 ]
2. Cursor execution: [ local-only for v1 / wire cloud option early — recommend local-only ]
3. Voice channel join: [ auto-join when I join / `!join` command — recommend `!join` ]
4. Gemini Live unavailable fallback: [ error out / Whisper+TTS fallback / text-only — recommend error out ]
5. Push-to-talk vs. open mic: [ open mic / push-to-talk — recommend open mic ]
6. Daily spend cap (`DAILY_SPEND_CAP_USD`): [ $___ — suggest $20 ]

**From the appendix:**

7. Email backend: [ Apple Mail / Gmail ]
8. Calendar backend: [ Apple Calendar / Google Calendar ]
9. Notes backend: [ Apple Notes / Obsidian / Notion / other: ___ ]
10. Tier W confirmation default: [ 30-day training then off / always on — recommend 30-day ]
11. Quiet hours (no proactive speech): [ ___ to ___ — suggest 10pm–7am ]
12. Priority senders (seed `user/contacts` memory): [ list 5+ names + emails ]
13. Domain inference router: [ use Haiku 4.5 / skip — recommend skip until tool catalog >30 ]
14. Browser MCP: [ Playwright / fetch+search only — recommend fetch+search first ]
15. Local model on PC when GPU arrives: [ revisit after Phase 9 ]

---

## Scope and Timeline

**MVP (Phases 0–3 of the main plan): ~10 working days.**
- Phase 0: setup (½ day)
- Phase 1: voice loop, no tools (3–4 days)
- Phase 2: plan-and-build loop with the four tools (3–4 days)
- Phase 3: hardening, error recovery, cost guards, auth (2–3 days)

**General-purpose capability (Phases 5–8 of the appendix): ~3 additional weeks.**
- Phase 5: MCP foundation + Apple ecosystem + `do_with_claude` (1 week)
- Phase 6: safety, audit, kill switch (3–4 days)
- Phase 7: memory expansion (3–4 days)
- Phase 8: workflows + scheduler (3–4 days)

**Phase 9 (wider tool fleet) and Phase 10 (proactive observation) are stretch.** Don't start them until 5–8 are solid in real daily use.

Total target: working MVP in two weeks, fully general-purpose in six weeks.

Push back hard if you think any phase is mis-estimated. Better I hear "this is two weeks not one" on day one than three weeks in.

---

## Definition of Done — Phase by Phase

These are the only acceptance gates. The phase isn't done until I can do exactly this:

**Phase 1:** I open Discord on my phone, type `!join` in the text channel. The bot joins the voice channel. I talk; it talks back with conversational latency (under one second for direct responses). I leave the voice channel; the bot leaves. No tools are working yet.

**Phase 2:** I speak: "Plan a refactor of the auth service in [project]. The token refresh is racy." The bot asks one or two clarifying questions. It calls Claude. I hear a summary; the full plan is in `#bot-text`. I say "but add idempotency keys." It calls Claude again with my feedback. I say "ship it." Cursor starts editing files on my Mac in a new branch. I hear progress narration. When Cursor finishes, I hear the summary; the diff is in the text channel.

**Phase 3:** I use it for an entire workday. The Gemini WebSocket drops once during the day; the bot reconnects without my noticing. Spend stays under the daily cap. Trying to trigger Cursor as a non-authorized Discord user is refused.

**Phase 5:** I say "what's in my inbox" and hear an accurate summary of my actual unread mail. I say "draft a reply to the most recent email from [name] saying I'll get back to them tomorrow"; the bot pauses, surfaces the draft for confirmation, I approve, the draft appears in my Drafts folder (not sent).

**Phase 6:** I say "send that email." Confirmation surfaces with the recipient, subject, and body preview. I say "no, change the subject to X." Claude revises. Confirmation surfaces again. I say "send." It sends. The action is in `data/audit.jsonl` with all details. I say `!stop` mid-build during a different task; it aborts.

**Phase 7:** I tell the bot three durable facts ("I'm allergic to peanuts," "my CTO is named Mike, his email is x@y.com," "I prefer concise email replies"). A week later, in a new session, the bot demonstrates it remembers all three when relevant tasks come up.

**Phase 8:** At 8am the next weekday, the bot speaks a morning briefing covering my actual calendar, unread email, and weather. I author a new workflow mid-day by saying "save this routine as 'meeting-prep'." Tomorrow at the scheduled time, it runs.

If any of these are unclear, ask me before you assume.

---

## Code Quality Bar

- Python 3.11+. Type hints throughout. `mypy --strict` should pass.
- One module per concern (`bot.py`, `discord_voice.py`, `gemini_session.py`, etc., as in the plan).
- Tests: unit tests for each tool handler. Smoke test for the full plan-and-build loop. Mock the external APIs; don't burn budget in CI.
- Logging: structured (JSON-line to stderr or a file). INFO by default. DEBUG triggered by env var.
- Secrets: only in `.env`. Never logged. Never committed. `.env.example` lives in the repo with empty values.
- Documentation: a README per directory explaining what's in it and how to run it. The top-level README covers install, first-run, and where to look when something breaks.
- Commits: small, focused, descriptive. Branch per phase if you want, or trunk-based — your call.

---

## What You Don't Build

Listed in the docs but worth restating because the last attempt got these wrong:

- No primary conversational model under Gemini.
- No agent framework or orchestrator above the tool dispatcher.
- No vector DB service (mem0's local store is enough).
- No multi-user system.
- No web UI / admin dashboard.
- No custom integrations where an MCP server exists.
- No "router model" or "meta-agent" — domain inference via Haiku is the entire routing layer, and only when tool catalog is large.
- No constructor / pattern detection loop (Phase 10 is stretch, not v1).

If you find yourself building one of these, stop and ask.

---

## Working Rhythm

- **Daily:** quick async update in [Slack channel / Discord / etc.] — what got done, what's next, blockers.
- **Phase gates:** demo the gate condition with me on a call. Don't declare a phase done without my eyes on it.
- **Blockers:** surface immediately. Don't sit on a stuck day. If an MCP server is broken, if a doc is wrong, if a non-negotiable conflicts with reality — flag in real time.
- **Pushback:** the docs are my best current thinking, not gospel. If you see a real problem, tell me. If you see a tradeoff, lay it out and recommend.

---

## What "Going Wrong" Looks Like

If by end of week one you don't have:

- A bot logging into Discord and joining a voice channel on command.
- An end-to-end voice round trip through Gemini Live with under-1s latency.
- A clear list of remaining decisions I owe you.

…then we're off track and we should reset. Tell me early.

If by end of week two you don't have:

- The full plan-and-build loop working end to end on a real project.
- Cost guards and authorization enforced.
- launchd supervision and auto-restart verified.

…then we're off track and we should reset.

Better one painful reset on day 10 than three weeks of compounding drift.

---

## Final Note

The architecture is small enough to hold in your head. That property is the design constraint. When you find yourself adding a layer, a middleware, a "manager," a "coordinator," a "router service" — stop. Ask whether a better prompt or a smaller tool subset solves it. Usually it does. The system is glue between four well-supported SDKs plus an MCP fleet. Keep it that way.

Ship something that works on real tasks. The rest follows.
