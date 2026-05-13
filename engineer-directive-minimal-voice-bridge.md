# Engineer Directive: Voice Bridge — Minimal Scope

**The current plan is too broad. Rebuild it to this scope. Anything outside this directive is out of scope; if you find yourself doing it, stop and ask.**

---

## The problem

Only one thing is broken: the Discord voice WebSocket handshake. py-cord fails close code 4017 because it does not implement DAVE. Everything else py-cord does — text channels, commands, message events, intents, threads, member events — works fine and has worked for a month.

## The fix

Replace **only** the voice WebSocket. Nothing else.

---

## What you DO NOT touch

- `src/bot.py` — leave it alone
- All py-cord commands (`!join`, `!leave`, `!stop`, `!status`, `!preflight`) — they work
- Text channel posting, thread creation, message handling — they work
- Intents, slash commands, message events — leave alone
- `requirements.txt` — keep py-cord, keep PyNaCl, keep everything
- All wiring fixes W1–W9 — unchanged
- `prompts/`, MCP code, Cursor wrapper, Gemini session, mem0, SQLite, config, ops, launchd plist — all untouched

If you are editing any file other than the two listed below, you are doing the wrong thing.

---

## What you build

### 1. New directory: `discord_voice_bridge/`

One Node.js process. ~100 lines. Owns the voice WebSocket and nothing else.

**`discord_voice_bridge/package.json`:**
- `discord.js`
- `@discordjs/voice`
- `opusscript` (pure JS — do not use `@discordjs/opus`, it requires native compilation)
- `prism-media`
- `libsodium-wrappers`

**`discord_voice_bridge/index.js`:**

- Logs in to Discord with a **second bot token** (separate Discord application, new env var `DISCORD_VOICE_BOT_TOKEN`).
- Intents: `Guilds`, `GuildVoiceStates`. Nothing else.
- Reads JSON-line commands on stdin:
  - `{"action": "join", "channel_id": "..."}` → join that voice channel
  - `{"action": "leave"}` → leave voice
  - `{"action": "play", "pcm_b64": "..."}` → upsample 24kHz mono PCM to 48kHz stereo via `prism-media` FFmpeg, push to `AudioPlayer`
  - `{"action": "shutdown"}` → destroy connection, exit
- Writes JSON-line events on stdout:
  - `{"event": "ready"}` on login
  - `{"event": "joined", "channel_id": "..."}` when voice connect succeeds
  - `{"event": "left"}` on disconnect
  - `{"event": "audio", "pcm_b64": "...", "user_id": "..."}` per received frame
  - `{"event": "error", "message": "..."}` on any failure
- Receive path: subscribe per speaker, decode opus, downsample 48kHz stereo to 16kHz mono via `prism-media` FFmpeg, emit `audio` events.
- **Filter on the Node side:** only emit `audio` events for the authorized user ID, read from env `AUTHORIZED_VOICE_USER_ID`. Drop everyone else.

### 2. Replace internals of `src/discord_voice.py`

Same Python file, same public API the rest of the code already calls. Internals change.

- Delete all py-cord `AudioSink`, `AudioSource`, `start_recording`, sink machinery.
- On init: spawn `node discord_voice_bridge/index.js` as a subprocess. Start a reader task on stdout.
- Public methods (preserve names so callers don't change):
  - `async def join(channel_id: str)` → write `{"action": "join", ...}` to subprocess stdin
  - `async def leave()` → write `{"action": "leave"}` to subprocess stdin
  - `async def send_audio(pcm: bytes)` (Gemini → Discord) → base64 encode, write `{"action": "play", ...}` to subprocess stdin
  - Register `on_audio_received(pcm: bytes)` callback → reader task invokes this on each `audio` event
- ~80 lines total.

### 3. `.env` additions

```
DISCORD_VOICE_BOT_TOKEN=<token for new bot>
AUTHORIZED_VOICE_USER_ID=<your Discord user ID>
```

### 4. Prerequisites (do these first)

- `brew install ffmpeg` — `prism-media` shells out to it; without it the bridge crashes on first audio frame
- Create a second Discord application called "voice-relay"; copy bot token; invite to the same server with permissions: Connect, Speak, Use Voice Activity
- `cd discord_voice_bridge && npm install`

---

## How the two bots split work

- **Bot #1 (existing, py-cord):** brain. Text channels, commands, events. Already works. Does not touch voice.
- **Bot #2 (new, Node):** voice only. Joins/leaves the voice channel. Streams PCM. Does nothing else. Never reads text, never handles commands.

Glued through the Python process: `bot.py`'s `!join` command calls `discord_voice.join(VOICE_CHANNEL_ID)` exactly like it does now. The Python wrapper writes one line to the Node subprocess. Node joins. PCM flows.

---

## Done means

1. Run the bot.
2. Type `!join` in the existing text channel (handled by py-cord, unchanged).
3. The Node subprocess joins the voice channel.
4. You speak. Audio arrives in Python. Gemini responds. You hear it.
5. Type `!leave`. Node leaves voice.
6. Type `!stop` during a build. It aborts. (This was already working — verify still works since nothing in that path changed.)

If `!join`, `!leave`, `!stop`, text posting, threads, or anything else that worked yesterday is now broken, **you went out of scope**. Revert and retry.

---

## Timeline

One day. Two days maximum.

If you are on day three, you are doing too much. Stop and tell me what you touched outside the two files listed above.

---

## What to ignore from the previous plan

- Do not rewrite `bot.py`
- Do not remove py-cord
- Do not create `discord_bridge.py` as a separate wrapper layer (just edit `discord_voice.py`)
- Do not move command parsing to Node
- Do not move text channel operations to Node
- Do not move thread operations to Node
- Do not register slash commands
- Do not touch wiring fixes W1–W9
- Do not change `requirements.txt`

Two files. One new Node sidecar. One Python file's internals replaced. That is the whole job.
