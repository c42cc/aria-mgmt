# Appendix A: Delivery Mechanism — Generate, Store, Link

## The Problem Restated

The product requires 2000–3000 words of output. The conversational interface (Discord — text or voice) is not a document reader. Streaming a wall of text into chat, or reading it aloud via TTS, confuses two things that should be separate: **commissioning a story** and **reading a story**.

## The Primitive

**The Discord bot is the remote control. It is not the screen.**

The story is an artifact — like a file, like a build output, like a generated report. You don't paste a PDF into Slack. You link to it.

```
Discord interaction     →  lightweight (seconds, a few messages)
Story artifact          →  heavy (2000-3000 words, stored server-side)
Reading experience      →  happens in browser, purpose-built for reading
```

## The Mechanism

```
1. User (Discord):   /spicylit "bondage, power exchange, my name is Alex"
                     — or via voice: "Hey, make me a spicy story about..."

2. Bot (Discord):    "✍️ Writing your story..."
                     (one ephemeral status message, or a spoken acknowledgment)

3. Backend:          Runs 2-pass pipeline (outline → story)
                     Stores result: { id, story_text, user_id, kinks, timestamp }

4. Bot (Discord):    Edits message → "Your story is ready."
                     Sends: [Read Story ↗] [Continue ▸] [New Story ▸]
                     — or speaks: "Your story's ready. I dropped the link in chat."

5. User (Browser):   Taps link → opens SpicyLit reader page → reads story
```

That's it. The Discord interaction is 2 messages and 10 seconds. The reading happens in a viewer built for reading. The bot never has to chunk, paginate, or stream text into chat.

## Why This Is The Right Decomposition

| Concern | Owner | Not Owner |
|---|---|---|
| Collecting user preferences | Discord bot | Web reader |
| Parsing input into structured fields | SpicyLit module | Discord bot |
| Generating the story (2-pass LLM pipeline) | SpicyLit module | Discord bot |
| Storing the story artifact | Backend / DB | Discord, Browser |
| Delivering the reading experience | Web reader | Discord bot |
| Continuation / new story decisions | Discord bot (buttons) | Web reader |

Each component does one thing. The Discord bot never touches story text except to pass input to the pipeline and receive a story ID back.

## The Reader

You already built it. The existing SpicyLit web app IS the reader — black background, white text, Lato font, scrollable story box, mobile-first. It just needs one change: instead of generating stories on-demand from its own input form, it fetches a stored story by ID.

### Minimal reader route

```python
@app.route("/story/<story_id>")
def read_story(story_id):
    story = db.get_story(story_id)
    if not story:
        abort(404)
    return render_template("reader.html", story=story)
```

The reader page is the existing Story Box UI — stripped of the input form, just the reading experience. Dark mode, full viewport, scroll to read. One URL, one page, one purpose.

### URL structure

```
https://spicylit.yourdomain.com/story/a1b2c3d4
```

Short, shareable (if they want), no auth required. If you want privacy, use long random UUIDs — unguessable but no login wall.

### Optional: expiry

Stories auto-delete after 24–72 hours. Keeps storage bounded, adds a "read it before it's gone" dynamic. Simple cron or TTL on the DB record.

## Storage

One table:

```sql
CREATE TABLE stories (
    id          TEXT PRIMARY KEY,   -- UUID
    user_id     TEXT NOT NULL,      -- Discord user ID
    story_text  TEXT NOT NULL,      -- The full generated story
    outline     TEXT,               -- Internal, for continuation context
    kinks       TEXT,               -- JSON array
    user_name   TEXT,
    created_at  TIMESTAMP DEFAULT NOW(),
    expires_at  TIMESTAMP           -- Optional TTL
);
```

This replaces the cookie-based state from the web MVP entirely. Prior story context for continuations comes from querying the user's most recent story row.

## Voice Integration

This decomposition maps perfectly to your existing voice architecture (Gemini Live → function calling → tool executor). The SpicyLit capability becomes a function tool:

```python
# Gemini Live function declaration
{
    "name": "generate_spicy_story",
    "description": "Generate a personalized adult story based on user preferences",
    "parameters": {
        "type": "object",
        "properties": {
            "preferences": {
                "type": "string",
                "description": "User's story preferences: name, kinks, themes"
            },
            "continue_previous": {
                "type": "boolean",
                "description": "Whether to continue the user's last story"
            },
            "adjustment": {
                "type": "string",
                "description": "Adjustments for continuation (e.g., 'more intense')"
            }
        },
        "required": ["preferences"]
    }
}
```

When Gemini calls this function:
1. Tool executor fires the 2-pass pipeline
2. Returns the story URL to Gemini
3. Gemini speaks: "Your story's ready — I put the link in your chat."
4. Bot posts the link in the text channel

The voice interaction is 15 seconds. The user reads at their own pace in the browser.

## What Your Engineers Build

### New code

1. **Story storage** — one DB table, two functions: `save_story()`, `get_story()`
2. **Reader endpoint** — one Flask route serving one HTML template (reuse existing SpicyLit CSS/layout)
3. **Discord command** — `/spicylit` slash command that calls the SpicyLit module and returns a link
4. **Gemini tool declaration** — one function definition for voice-triggered generation
5. **Cleanup job** — optional cron to delete expired stories

### Existing code reused as-is

- The two prompt templates (`get_outline_prompt`, `get_story_prompt`)
- The xAI API client
- The input parser
- The SpicyLit CSS/reading experience

### Code NOT written

- No Discord message chunking logic
- No pagination state machine
- No embed builder
- No streaming-to-chat pipeline
- No TTS of story content
- Nothing that treats Discord as a document viewer

## Summary

The answer to "how do I deliver 3000 words through a conversational interface" is: **you don't.** You deliver a link through the conversational interface and 3000 words through a reader. The conversation is the commissioning. The browser is the consumption. They're different activities and they belong in different places.
