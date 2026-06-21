# Aria

Aria is the single conversation through which Corbin gets real work done — by
talking. You say what you want at the level you'd tell a chief of staff; she
understands it, asks only what she still needs, reads the plan back, and on your
go, makes it happen and tells you the truth about what landed.

> One calm voice you talk to. She thinks. One engine does the work. You trust
> that what she says happened, happened.

## What she is

- **The conductor — Claude.** She owns the conversation's *content*: she
  understands the intent, picks the right loop, decides what to ask, and judges
  whether the plan is right. This is the thing v1 got wrong (it let a fast,
  non-reasoning front door decide), and it is why v1 failed. Now the smart model
  is the one in the conversation.
- **The voice — fast transport.** A voice layer (LiveKit + a fast model) is her
  ears and mouth: it hears you, renders her words, handles turn-taking and
  letting you interrupt. It never decides anything. Her identity lives in her
  words, not the wire.
- **The engine — Claude Code.** One body does the building and the doing. No
  Cursor, no fleet of brains and bodies to keep separate — one engine, reached
  the same way for every kind of work.

## How she behaves (the promises that make her trustworthy)

- **One thing at a time, briefly.** She asks one question, keeps turns short,
  and yields the instant you talk over her. Silence is her acknowledgment.
- **She never claims what she can't see.** If the engine didn't confirm it, she
  says plainly what she tried and the one thing she needs — never a confident
  "done" that wasn't. A crisp blocker beats a comfortable lie.
- **She remembers, so she doesn't re-ask.** Durable facts pre-fill what she'd
  otherwise have to ask. Three questions the first time; one by the tenth.
- **Nothing fires without your go.** The go-gate is mechanical, not a matter of
  her being careful.
- **Done means verified.** A request reads as done only when a real result was
  checked against ground truth — and the running outcome log, reviewed by you,
  is the only metric that matters.

## What she can do today

Talk to her in text, describe a scoped code change in one of your projects, and
she runs the **Feature Build** loop: she interviews, confirms, and on your go
dispatches Claude Code on a fresh branch, runs the tests, and reports the diff —
honestly, verified. Capability grows by adding a loop file and an endpoint, never
by growing her core.

## Where she's going

The phone call (talk to her like a person — Phase 1), a library of loops
(software work and house work as the same object pointed at different engines),
a core that travels from your Mac to a home server to the cloud over a secure
mesh, the house, and proactive stewardship — she initiates, not just responds.
The shape never changes: one conductor, capability as data, verified outcomes.

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the map and `docs/aria-v2/` for
the build decisions and the phased plan.
