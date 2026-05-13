# SpicyLit → Discord Bot Integration Brief

## What This Document Is

A single engineering request for the Discord bot team. After reading this, you should have everything needed to integrate SpicyLit's adult story generation capability into our Discord bot. No ambiguity, no back-and-forth needed.

---

## 1. What SpicyLit Actually Is (The Primitive)

Strip away the web UI, the cookies, the CSS — SpicyLit is a **two-pass text generation pipeline**:

```
User Input → Parse(name, kinks) → Pass 1: Generate Outline → Pass 2: Generate Story → Streamed Text Output
```

That's it. Everything else in the existing web app (Flask routes, SSE streaming, cookie state, PWA manifest) is delivery mechanism. The capability you're integrating is the pipeline above.

### The Two Passes

**Pass 1 — Outline Generation**
- Input: user preferences (name, kinks, word target, optional prior story context)
- Output: a scene-by-scene outline (~500 words)
- Purpose: gives the story structural coherence before the long-form write

**Pass 2 — Story Generation**
- Input: the outline from Pass 1 + same user parameters
- Output: the full story (~2000–3000 words), streamed
- Purpose: the actual deliverable the user reads

Both passes hit the same LLM endpoint. The outline is consumed internally (user never sees it). Only the story output is delivered.

### What the LLM Needs

The underlying API is **xAI Grok** (currently Grok 4). The integration requires:
- An `XAI_API_KEY` (stored as env var / secret — never hardcoded)
- Endpoint: standard xAI chat/completions API
- Streaming support (SSE-style chunked responses)
- `max_tokens`: ~4000 per pass

---

## 2. Inputs & Outputs (The Interface Contract)

### Inputs from the user

| Field | Source | Required | Example |
|---|---|---|---|
| `user_name` | Parsed from input or default "You" | No | "Alex" |
| `kinks` | Parsed from input (comma-sep or NLP) | Yes | ["bondage", "power exchange"] |
| `is_continuation` | Explicit user choice | No (default: false) | true |
| `adjustment` | Free text when continuing | No | "more intensity, slower buildup" |
| `prior_story` | Retrieved from state store | Auto | (last story text) |
| `prior_kinks` | Retrieved from state store | Auto | (last kink list) |

### Outputs to the user

| Field | Type | Notes |
|---|---|---|
| `story_text` | string (2000–3000 words) | The generated story |
| `outline` | string | Internal only — do not surface to user |

### Derived Constants (Defaults)

- `target_pages`: 2
- `target_words`: 2500
- `perspective`: first person, user as submissive protagonist

---

## 3. Discord-Specific Constraints & Design Decisions

The web app didn't have to deal with any of these. Your team does.

### 3.1 Message Length (The Big One)

Discord messages cap at **2000 characters**. A SpicyLit story is 2000–3000 *words* (~12,000–18,000 characters). You have three realistic options:

| Approach | Pros | Cons |
|---|---|---|
| **A. Thread + chunked messages** | Native Discord UX, searchable, persistent | Lots of messages (6–9 per story), notification noise |
| **B. Thread + single file attachment** | Clean, one deliverable, no char limit | User must open file, breaks immersion |
| **C. Thread + paginated embeds with buttons** | Controlled reading pace, interactive | More complex to build, state management |

**Recommendation: Option A (thread + chunked messages)** for MVP. It's the simplest to implement, maps naturally to streaming, and keeps the user in Discord. Chunk at paragraph boundaries, not at the 2000-char wall mid-sentence. Add a small delay (1–2s) between chunks to simulate the "story unfolding" feel.

### 3.2 Interaction Model

Map the web UI's engagement loop to Discord primitives:

```
Web UI Concept        →  Discord Equivalent
─────────────────────────────────────────────
Initial input box     →  Slash command: /spicylit "your preferences here"
Generate button       →  Command execution (auto-triggers)
Story box             →  Dedicated thread (auto-created per story)
Continue/New toggle   →  Buttons at end of story: [Continue ▸] [New Story ▸]
Adjustment input      →  Modal popup (Discord modal) triggered by button
Reset                 →  /spicylit-reset or button
```

### 3.3 State Management

The web app uses cookies. Discord has no cookies. You need server-side state per user.

**Minimum state to persist per user:**

```json
{
  "user_id": "discord_user_id",
  "prior_story": "last generated story text (truncate to last 2000 chars for prompt context)",
  "prior_kinks": ["kink1", "kink2"],
  "last_generated_at": "ISO timestamp",
  "active_thread_id": "discord_thread_id"
}
```

**Storage options** (pick based on your bot's existing infra):
- SQLite if the bot runs on a single server
- Redis if you want TTL-based auto-expiry (stories expire after X hours)
- PostgreSQL if you already have it in the bot stack
- In-memory dict if this is a prototype (loses state on restart)

### 3.4 Rate Limiting

The web app does 5 req/min per IP. For Discord, rate limit per `user_id`:
- **Suggested**: 3 generations per 10 minutes per user
- Enforce at the bot command layer, not the API layer
- Return a friendly message: "Your story is still warm — wait a few minutes before generating another."

### 3.5 Age Gating

This is adult content on a platform with minors. **Non-negotiable requirements:**
- The bot command must **only work in channels marked as NSFW** (`channel.is_nsfw()`)
- If invoked in a non-NSFW channel, reply ephemerally: "This command only works in age-restricted channels."
- Consider also checking if the server has age verification enabled

---

## 4. The Prompt Templates

These are the two core functions your bot needs. They are the actual prompt engineering — copy them into your codebase as-is and call them in sequence.

### 4.1 Outline Prompt Generator

```python
def get_outline_prompt(
    target_pages: int,
    user_name: str,
    kinks: list,
    target_words: int,
    prior_story: str = None,
    prior_kinks: list = None,
    is_continuation: bool = False
) -> str:
    if is_continuation:
        return f"""Create a detailed outline for a {target_pages}-page CONTINUATION of an erotic BDSM story.

Previous story:
{prior_story}

Previous kinks explored: {', '.join(prior_kinks) if prior_kinks else 'None'}
New kinks to incorporate: {', '.join(kinks)}

Perspective: Continue first-person from {user_name} (submissive)

Structure the SEQUEL outline with:
1. Opening that references events from the previous story
2. Escalation beyond the previous story's intensity
3. Introduction of new elements/kinks while building on established dynamics
4. {3 + (target_pages // 5)} escalating mini-arcs that go deeper than before
5. Major climax that surpasses the previous story
6. Resolution that shows growth/change from the beginning

This is a CONTINUATION, not a retelling. Build on what happened before.
Include callbacks to the prior story but focus on NEW experiences.
Target length: {target_words} words total.

Provide a scene-by-scene breakdown."""

    else:
        return f"""Create a detailed outline for a {target_pages}-page erotic BDSM story in the style of Reddit erotica.

Perspective: First-person from {user_name} (submissive)

Kinks to incorporate: {', '.join(kinks)}

Structure the outline with:
1. Opening hook - everyday situation with tension
2. Character introductions with power dynamics
3. {3 + (target_pages // 5)} escalating mini-arcs of teasing/humiliation/submission
4. Major climax scene with psychological and physical intensity
5. Resolution/aftermath with lingering effects

Include specific plot points, dialogue snippets, and psychological moments.
Make it raw, believable, and intensely erotic.
Target length: {target_words} words total.

Provide a scene-by-scene breakdown."""
```

### 4.2 Story Prompt Generator

```python
def get_story_prompt(
    target_pages: int,
    user_name: str,
    kinks: list,
    outline: str,
    prior_story: str = None,
    is_continuation: bool = False
) -> str:
    if is_continuation:
        start = f"Write a {target_pages}-page CONTINUATION/SEQUEL of an erotic BDSM story following this outline:"
    else:
        start = f"Write a {target_pages}-page erotic BDSM story following this outline:"

    prior_context = ""
    if prior_story:
        truncated = prior_story[-2000:] if len(prior_story) > 2000 else prior_story
        prior_context = f"""Previous story context (for reference but don't repeat):
{truncated}
"""

    prompt = f"""{start}

{outline}

First-person perspective from {user_name} (submissive).

{prior_context}

Key Elements:
- {'New' if is_continuation else 'Niche'} Kinks: {', '.join(kinks)}. {'Build on previous kinks while adding new ones.' if is_continuation else 'Integrate gradually with psychological impact.'}
- Tone: Raw, confessional, immersive. Like Reddit erotica - imperfect but authentic.
- Style: Include subtle flaws like 'I really didn't knew what to say' for authenticity.
- Length: Start with a strong 2000-3000 word opening section.
- Sensory: Focus on feelings, sounds, physical sensations, internal conflict.
- Dialogue: Natural, flawed, with hesitations and real speech patterns.
{f'- Continuity: Reference prior events naturally, show progression/escalation from before.' if is_continuation else ''}

Begin the {'continuation' if is_continuation else 'story'} now. Make it extremely erotic with no limits. Start with {'a hook that connects to the previous story' if is_continuation else 'an engaging hook'}."""

    return prompt
```

---

## 5. Generation Pipeline (Pseudocode)

This is the core logic your bot executes when a user triggers a story:

```python
async def generate_story(user_input: str, user_id: str, is_continuation: bool = False):
    # 1. Parse input
    user_name = extract_name(user_input) or "You"
    kinks = extract_kinks(user_input)  # comma-split or NLP

    # 2. Load prior state (if continuing)
    state = await load_user_state(user_id)
    prior_story = state.get("prior_story") if is_continuation else None
    prior_kinks = state.get("prior_kinks") if is_continuation else None

    # 3. Pass 1: Generate outline (non-streamed, internal)
    outline_prompt = get_outline_prompt(
        target_pages=2,
        user_name=user_name,
        kinks=kinks,
        target_words=2500,
        prior_story=prior_story,
        prior_kinks=prior_kinks,
        is_continuation=is_continuation
    )
    outline = await call_xai(outline_prompt, stream=False)

    # 4. Pass 2: Generate story (streamed to Discord)
    story_prompt = get_story_prompt(
        target_pages=2,
        user_name=user_name,
        kinks=kinks,
        outline=outline,
        prior_story=prior_story,
        is_continuation=is_continuation
    )
    story_text = await call_xai_streamed_to_discord(
        prompt=story_prompt,
        thread=thread,
        chunk_at="paragraph"  # split at \n\n, not at char limit
    )

    # 5. Save state
    await save_user_state(user_id, {
        "prior_story": story_text[-2000:],  # keep last 2000 chars
        "prior_kinks": kinks,
        "last_generated_at": now()
    })

    # 6. Append interaction buttons
    await thread.send(view=StoryButtons())  # [Continue ▸] [New Story ▸]
```

---

## 6. Architecture Diagram

```
User (Discord)
  │
  ├─ /spicylit "bondage, power play"
  │
  ▼
Discord Bot (your existing bot)
  │
  ├─ NSFW channel check
  ├─ Rate limit check
  ├─ Parse input → (user_name, kinks)
  ├─ Load user state (DB/Redis)
  │
  ├─ [SpicyLit Module] ◄── NEW CODE LIVES HERE
  │     │
  │     ├─ get_outline_prompt() → call xAI API → outline
  │     ├─ get_story_prompt(outline) → call xAI API (streaming) → story chunks
  │     └─ return chunks
  │
  ├─ Create/reuse thread
  ├─ Stream chunks → thread messages (paragraph-split)
  ├─ Append [Continue ▸] [New Story ▸] buttons
  └─ Save user state
```

The **SpicyLit Module** should be a self-contained Python module (single file or small package) that the bot imports. It owns the prompts, the xAI API calls, and the input parsing. It should have zero knowledge of Discord — it takes strings in and yields strings out. The bot layer handles all Discord-specific concerns (threads, buttons, modals, permissions).

---

## 7. What to Build (Task Breakdown)

### Phase 1: Core Integration (MVP)
1. **SpicyLit module** (`spicylit.py` or `spicylit/`): prompt templates, xAI API client, input parser
2. **Slash command** `/spicylit`: accepts a string argument, runs pipeline, outputs to thread
3. **Thread management**: auto-create a thread per story, name it something relevant
4. **Chunked output**: split story at paragraph boundaries, send as sequential messages
5. **NSFW gate**: reject in non-NSFW channels
6. **Rate limiting**: 3 per 10 min per user

### Phase 2: Interaction Loop
7. **Buttons**: `[Continue ▸]` and `[New Story ▸]` at end of story
8. **Modal**: on button press, open a Discord modal for adjustment/new preferences
9. **State persistence**: store prior_story + prior_kinks per user (SQLite or whatever your bot uses)
10. **Continuation flow**: wire the modal input back through the pipeline with `is_continuation=True`

### Phase 3: Polish
11. **Loading indicator**: "✍️ Crafting your story..." status message while outline generates
12. **Error handling**: friendly messages on API failure, retry logic (1 retry then fail gracefully)
13. **Input sanitization**: strip any Discord markdown injection, limit input to 500 chars
14. **Slash command for reset**: `/spicylit-reset` clears user state

---

## 8. Environment & Secrets

The bot needs one new secret:

| Key | Description | Where to Store |
|---|---|---|
| `XAI_API_KEY` | xAI / Grok API key | Same secret store as your bot token |

Do **not** store this in code, config files, or version control.

---

## 9. Open Questions for the Team

These are decisions the Discord bot team should make based on your existing bot architecture:

1. **Which DB/store** are you already using for bot state? Use that for SpicyLit user state too.
2. **Should stories be ephemeral?** Auto-delete threads after N hours? Or persist?
3. **Multi-server isolation**: if the bot is in multiple servers, should user state be per-server or global?
4. **Concurrency**: how many simultaneous xAI API calls can you sustain? The outline + story sequence means 2 API calls per generation. Plan for queuing if needed.
5. **Input parsing sophistication**: the web app does basic comma-split for kinks. Do you want something smarter (e.g., let the LLM itself parse the input into structured fields)?

---

*Document version: 1.0 — ready for handoff.*
