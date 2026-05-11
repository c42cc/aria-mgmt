# UCS — Unified Conversation System

Talk to your software development workflow from your phone. One Python process on your Mac connects Discord voice to Gemini 3.1 Live (conversational layer), which dispatches work to Claude Opus 4.6 (planner) and Cursor (builder) via function calls.

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
│   ├── tools.py            # Four tool implementations
│   ├── cursor_bridge.py    # Python ↔ Node subprocess bridge
│   ├── memory.py           # mem0 wrapper
│   ├── db.py               # SQLite schema + queries
│   ├── prompts.py          # Prompt template loader
│   └── config.py           # Env loading, defaults
├── cursor_wrapper/         # Node.js bridge to @cursor/sdk
├── prompts/                # Markdown prompt templates
├── projects/               # Project registry
├── workflows/              # Saved workflow descriptions
├── data/                   # Local state (gitignored)
├── ops/                    # launchd config, setup scripts
└── tests/                  # Smoke tests
```

## Discord Setup

1. Create a Discord application at https://discord.com/developers/applications
2. Add a Bot user, save token to `.env` as `DISCORD_BOT_TOKEN`
3. Enable intents: Server Members, Message Content, Voice State
4. Invite bot with scopes `bot` + `applications.commands`
5. Create channels: `#voice-bot` (voice), `#bot-text` (text), `#bot-logs` (text)
6. Copy channel IDs to `.env`

## Build Phases

- **Phase 0**: Bot logs in, joins/leaves voice channel
- **Phase 1**: Fluid voice conversation through Discord via Gemini
- **Phase 2**: End-to-end plan-and-build via voice
- **Phase 3**: Error recovery, cost guardrails, authorization
- **Phase 4**: Prompt iteration and workflow saving (ongoing)
