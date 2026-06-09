# Forensic: Aria's failures traced to one primitive — and the fix that moots them

**Date:** 2026-06-08
**Scope:** every judged Aria session in `data/verdicts.ndjson` (72 verdicts)
cross-referenced against `data/state.db::session_records`.
**Verdict tool:** `src/judge.py` (Gemini-Flash judge + deterministic anchor floor).

## Headline

| | |
|---|---|
| Judged sessions | 72 |
| `correct` | 22 |
| `degraded` | 10 |
| `failed` | 40 |
| **Real (non-fixture) failures** | **47** |
| **…that share ONE `session_key`** | **36** |

Thirty-six of the forty-seven real failures carry the **same**
`session_key = 1499307393417478295`. That number is the **`#ucs` channel id**.
Every request Corbin made in `#ucs` — unrelated tasks, minutes or hours
apart — was filed under one session. The remaining 11 are synthetic judge
fixtures (`test-*`, `proposal:*`) and one other channel.

That single fact is the whole story. The symptoms below are not separate
bugs; they are what one shared session does to concurrent, unrelated work.

## The dysfunctional primitive

> **A request's identity was its channel, not the request.**

`session_key` was set to `str(message.channel.id)` in both text entry points
(`_handle_text_conversation`, `_run_ask`). `session_key` is the key for:

- the agent **lock** (`tools._agent_lock_for`) — one lock per key, and
- the **context window** (`conversation.as_claude_context`) — one buffer, and
- the **progress sink** routing (`_emit_progress_to_user`).

So one channel ⇒ one lock ⇒ one context ⇒ one trail, shared by every request
that ever lands in it. Three failure classes fall straight out of that.

## Symptom → primitive map

### A. Collision — "an agent loop is already running" (4 direct, root of ~36)

Sessions **127, 128, 144** returned, verbatim:

```json
{"error": "An agent loop is already running for this session. Wait for it to finish or use !stop."}
```

`tools._do_with_claude` rejected a second call whenever the channel's lock was
held. Two requests in `#ucs` could not coexist. Session **136** timed out for
the same reason (contended shared session). The judge (spec
`specs/correctness/agent.md`: "'I couldn't do it' responses are FAILED") scored
each 0.0.

This rejection was a **symptom-handler**: code whose only job was to emit a
clean error for a collision the design guaranteed. Per our own rule
("recombine the system that captures the error with the one that produces it
into a single primitive"), it had to be deleted, not reworded.

### B. Context bleed — answers from the wrong request (e.g. 146, 124, 137)

With one buffer per channel, request B inherited request A's preamble. Session
**146**'s ask was *"send it to a new thread with Claude in browser"*; its stored
output is an answer about **camera continuity threads** — a different request's
result, bled across. Sessions **124/137** answered from stale Cursor-watch
context instead of acting on the actual ask. The chatty `live_visuals_4`
watcher firehose filled the shared buffer (already partially capped by
`_MAX_CURSOR_EVENTS_IN_CLAUDE_CONTEXT`, but the buffer was still global).

### C. The ack lied (every text request)

The ack said *"I'll show each step here"* while `_emit_progress_to_user` routed
every step to **`#ucs-alerts`**, a different, silent channel. The user watched
an empty `#ucs` while the work scrolled past somewhere they were told not to
look.

### D. Capability gaps the agent *had* but couldn't reach (141, 140, 146, 142)

- **141 / 140** — *"show me the full last message of that thread"* → *"it's just
  truncated there"* / iteration-limit. The transcript lives on disk and
  `cursor_read` returns it, but the **agent loop's** tool catalog
  (`_LOCAL_TOOL_SCHEMAS`) didn't lead with read-full discipline.
- **146** — *"spawn a new Cursor / Claude thread"* → *"I can't spawn new Cursor
  IDE agents."* `_cursor_spawn` existed and was wired for **voice**, but was
  **absent from the text agent's `_LOCAL_TOOL_SCHEMAS`/`_HANDLERS`.** Text-Aria
  was strictly less capable than voice-Aria for no reason.
- **142** — *"push the build button"* → *"I can't literally push the build
  button"* — an "I can't" that the spec auto-fails.

## The fix — one re-keying, plus the cleanups it unlocks

> **A request's identity is its own Discord thread. `session_key = thread.id`.**

Every top-level message opens a thread (the opener stays in `#ucs` as a clean
index entry); a message typed inside a thread continues it. Because the key is
now the thread, each request gets its **own** lock and its **own** context
window — collisions and bleed become structurally impossible.

| Change | File | Moots |
|---|---|---|
| `session_key = thread.id`; open a thread per request | `src/bot.py` `_ensure_work_thread`, `_handle_text_conversation`, `_run_ask` | A, B (root) |
| Delete the "already running" rejection; the per-thread lock **serializes** a same-thread follow-up instead | `src/tools.py` `_do_with_claude` | A |
| Scope agent context to `session_key`; voice still reads the whole buffer | `src/conversation.py` `Turn.session_key`, `as_claude_context(session_key=…)` | B |
| Progress steps + ack + answer all post **into the thread** | `src/bot.py` `_emit_progress_to_user`, `_send_chunked` | C |
| Expose `cursor_spawn` + `cursor_agents` to the agent loop; prompt: spawn / read-full / "doing beats explaining" | `src/tools.py` `_LOCAL_TOOL_SCHEMAS`/`_HANDLERS`, `prompts/do_with_claude_system.md` | D (141, 140, 146) |
| Durable thread↔session binding | `src/db.py` `bind_thread`, `session_for_thread` | restart-safety |

"Claude in browser thread" (146) is **unified into the spawn primitive**: a
Cursor thread *is* a fresh Claude-backed agent in its own thread, readable in
full and steerable — so "start a new Claude thread on this" maps to
`cursor_spawn`. We did **not** add a fragile claude.ai DOM driver; that would
be exactly the kind of timeout/storm-prone bloat our rules forbid.

## Why this is the few-moving-parts root fix

Re-keying from channel→thread is **one** change to **one** identifier. It does
not patch symptoms A–D individually; it removes the shared substrate they all
grew from. The collision rejection, the alerts-routing of steps, and the
global-buffer-as-context are all deleted, not maintained.

## Verification

Primitive-isolation tests (`tests/test_thread_per_request.py`, 14/14):

- same `session_key` **serializes** and both complete — no rejection envelope;
- distinct threads run **in parallel** (`max_concurrent == 2`);
- `as_claude_context(session_key=…)` shows only that thread's turns; a new
  thread is a clean slate; unscoped (voice) behavior unchanged;
- a top-level message **opens a thread**, runs under `session_key == thread.id`,
  posts ack + answer **into the thread**, leaks nothing to the parent channel;
- a follow-up inside a thread reuses the session, opens no new thread;
- progress steps post **into the thread**, falling back to `#ucs-alerts` only
  when there is no thread (voice / global);
- `cursor_spawn` + `cursor_agents` are present in the agent loop's catalog;
- `bind_thread` / `session_for_thread` round-trip and are idempotent.

Regression + wiring: `tests/test_dedup_and_dispatch.py` + `test_boundary_contracts.py`
(52/52) and `tests/smoke.py` (all green) — the conversation-buffer and
in-flight-loop guarantees still hold and the bot imports/wires cleanly.

Live golden-path (`make e2e-golden`, S10 text turn) asserts via the events
table + conversation buffer, both still populated, so its checks remain valid;
it requires a bot restart onto this code.
