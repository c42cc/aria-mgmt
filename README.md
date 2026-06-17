# UCS — Unified Conversation System

Talk to your software development workflow from your phone. One Python process on your Mac connects Discord voice to Gemini 3.1 Live (conversational layer), which dispatches work to Claude Opus 4.6 (planner) and Cursor (builder) via function calls.

The bot appears in Discord as **Aria**. She is the only non-human in the server and manages every channel and thread herself.

## Architecture

Three intelligences, strict roles:

- **Gemini 3.1 Live** — sole conversational layer (voice + text)
- **Claude Opus 4.6** — sole reasoner (planning, analysis, architecture)
- **Cursor SDK** — sole builder (code edits, tests, execution)

One process. No framework. ~600 lines Python + ~80 lines JS.

## Quick Start

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Install Node.js dependencies for Cursor bridge
cd cursor_wrapper && npm install && cd ..

# Copy and fill in secrets
cp .env.example .env
# Edit .env with your API keys

# Run
python -m src.bot
```

## Project Structure

```
ucs/
├── src/                    # Python source
│   ├── bot.py              # Main entry point
│   ├── discord_voice.py    # Voice channel I/O, PCM resampling
│   ├── gemini_session.py   # Gemini Live WebSocket + function calls
│   ├── tools.py            # Tool implementations + UCS dispatch
│   ├── ucs.py              # UCS Intelligence Loop, Model Router (UCS_ENABLED=true)
│   ├── eval.py             # Offline prompt evaluation CLI
│   ├── cursor_bridge.py    # Python ↔ Node subprocess bridge
│   ├── memory.py           # mem0 wrapper
│   ├── db.py               # SQLite schema + queries
│   ├── prompts.py          # Prompt template loader + versioning
│   └── config.py           # Env loading, defaults
├── cursor_wrapper/         # Node.js bridge to @cursor/sdk
├── prompts/                # Markdown prompt templates (versioned)
├── models.yaml             # Model registry (costs, capabilities, keys)
├── projects/               # Project registry
├── workflows/              # Saved workflow descriptions
├── data/                   # Local state (gitignored)
├── ops/                    # launchd config, setup scripts
└── tests/                  # Smoke tests
```

## Discord Setup

Aria runs as two Discord applications sharing one identity:

- **Aria (text bot)** — py-cord, handles text commands, threads, and tool dispatch.
  Token in `.env` as `DISCORD_APP_BOT_TOKEN`.
- **Aria (voice sidecar)** — discord.js Node process (`discord_voice_bridge/`),
  handles only the voice WebSocket. Token in `.env` as `DISCORD_VOICE_BOT_TOKEN`.

Both applications are invited to the same guild and named "Aria" in the
Discord developer portal, so they appear as one bot to the user.

Channels (three total):

- `#ucs` (text) — plans, build output, tool results, file attachments
- `#ucs-alerts` (text) — preflight reports, confirmations, errors
- `#general` (voice) — natural-language conversation with Aria
- `#spicy-lit` (text) — SpicyLit outline output

Channel IDs live in `.env` as `DISCORD_TEXT_CHANNEL_ID`, `DISCORD_LOG_CHANNEL_ID`,
and `DISCORD_VOICE_CHANNEL_ID`. Renaming channels in Discord does not break
anything because the code reads IDs, not names.

## Build Phases

- **Phase 0**: Bot logs in, joins/leaves voice channel
- **Phase 1**: Fluid voice conversation through Discord via Gemini
- **Phase 2**: End-to-end plan-and-build via voice
- **Phase 3**: Error recovery, cost guardrails, authorization
- **Phase 4**: Prompt iteration and workflow saving (ongoing)
