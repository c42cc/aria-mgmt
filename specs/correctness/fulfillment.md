# Correctness Spec — Fulfillment (request → true intent)

This spec judges whether Aria **fulfilled what the user actually meant**, not the
literal surface words and not the surface domain of what she did. It exists
because the shipped `agent` judge — and the first draft of this harness — both
made the same mistake: they judged the *literal* request and the *surface*
action, called a confabulated tangent "relevant," and missed a catastrophic
intent-misread (the R5 "Give me the debrief" forensic, below).

The unit of evaluation is **the arc**: the request PLUS everything needed to
understand it — the cross-channel context that resolves its referents, the
dispatch input that was actually handed to the engine, the tool trace of what
was actually done, and the response. A request is **never** judged without its
referent.

You receive a SESSION RECORD whose `## Arc` section contains:

- **Asked** — the user's verbatim request.
- **Dispatched task** — the exact task string the engine received (this may or
  may not include a "Recent conversation thread:" preamble; its presence or
  absence is itself evidence — see Stage 1).
- **Antecedent window** — the cross-channel turns (user / aria / cursor-watch /
  alert) that occurred in the minutes BEFORE the request. This is where a
  referent like "the debrief", "this", "the results", "it" points. The most
  recent cursor-watch "completed / produced an assistant turn" event is the
  prime candidate antecedent for a "what happened?" style ask.
- **Surroundings** — what was around her where she engaged: the conversational
  surface the user can see (recent messages + shared files/attachments), her
  recent artifacts, and her active watched work. A referent to "what's around"
  ("the panther video", "that file we shared") resolves here.
- **Corpus of access** — the tools/capabilities Aria had available.
- **Tool trace** — every tool call she actually made, with results.
- **Response** — what she returned to the user.

You judge in **two stages**, and you must show your work for both.

---

## Stage 1 — Reconstruct the TRUE intent (do this first, always)

From the arc, state:

1. **`true_intent`** — what the user actually meant, in one sentence. Resolve
   every referent against the antecedent window. "Give me the debrief" with a
   cursor-watch "task completed in live_visuals_4" 28 seconds earlier means
   *"debrief me on the Cursor work that just finished,"* NOT *"run a generic
   daily standup."*
2. **`referent`** — the specific antecedent the request points at (quote it from
   the window), or `none` if the request is self-contained.
3. **`could_resolve`** — could the **dispatch** have resolved the referent? Look
   at the **Dispatched task**:
   - If a referent clearly existed in the antecedent window but the dispatched
     task is the *bare* user words with no preamble carrying that antecedent,
     then the engine was **context-starved**: the antecedent never reached it.
     This is a **dispatch-layer** fact, established before you judge the
     reasoning.
   - If the dispatched task DID carry the antecedent (a preamble naming the
     cursor-watch event / prior turns), the engine had what it needed and any
     misread is a **reasoning-layer** fact.

Stage 1 is mandatory. A verdict that does not first state the reconstructed
true intent and its referent is itself an instance of the failure this spec
exists to kill, and must be treated as `unverified`.

---

## Stage 2 — Score against the TRUE intent, classify, attribute root cause

### Relevance is to the true intent, never the surface domain

> An action in the *same domain* as a **misread** of the request is NOT
> relevant. Forensic 2026‑06‑24: "Give me the debrief" pointed at a
> just-finished Cursor task; Aria answered with `search_emails newer_than:1d`
> (which returned unrelated Anthropic billing spam) and a Calendar permission
> wall. The shipped judge wrote *"performed a relevant email search."* That is
> the textbook error: **relevance to a confabulated reading is not relevance.**
> An email search is OFF‑THE‑RAILS for a "debrief the cursor work" intent.

### Delivery and awareness — the two halves of presence

- **Delivery (artifact requests).** When the user asks for an artifact to be
  **sent / brought / shown / delivered** ("send me the mp4", "bring me the file
  right here"), the ONLY fulfillment is the **delivered artifact in their
  channel** (the trace shows an actual attach/upload that landed). A description,
  a file path, a list of candidates, "I can't render it inline", or an offer to
  "open it on the Mac / send it via iMessage / email" is a **deflection, not
  fulfillment** — score it a failure. If she had no means to deliver, the
  root-cause is `capability-gap`.
- **Awareness (referents to her surroundings).** A reference to something around
  her — "the panther video", "that file we shared", "the thing we were looking
  at" — should be resolved from her **awareness of what's around her** (the
  Surroundings section: her recent artifacts, the conversational surface, her
  watched work). Resolving it with a blind filesystem `find`/`execute_command`
  and then asking "which one?" is a failure of awareness. If the resolving signal
  was absent from her Surroundings, the root-cause is `capability-gap` (she
  lacked ambient awareness); if it was present and she ignored it,
  `engine-reasoning`.

### Sub-scores (0.0–1.0, each cited)

- **`intent_match`** — does what she did line up with the TRUE intent (not the
  literal words)? An action serving a misread scores near 0 here even if it ran
  cleanly.
- **`completeness`** — of the achievable part of the true intent, how much did
  she deliver?
- **`effectiveness`** — judged WITH the corpus-of-access + trace. Penalize a
  halt ONLY for paths she actually **held** and did not try (a wall is a
  hypothesis, not a stop). Do NOT penalize her for a path she did not have.
- **`concision`** — did the response deliver the substance without padding? A
  confident, verbose response that did not do the work is worse than a short
  honest blocker, never better.

### Class (exactly one)

- **`FULFILLED`** — served the true intent; the result contains/achieves it.
- **`PARTIAL`** — achieved part of the true intent and honestly left the rest,
  without fabricating.
- **`OFF-THE-RAILS`** — acted on a confabulated / misread intent; the work is
  irrelevant to what the user meant. The signature failure (R5).
- **`BLOCKED-AVOIDABLE`** — honestly halted, but a capability she **held** would
  have gotten further; she stopped short (tried one node/user and quit). Honest
  but shallow.
- **`BLOCKED-UNAVOIDABLE`** — honestly halted on a genuine external wall with no
  held path, and named the one-command fix. Honesty here is CORRECT behavior.
- **`FABRICATED`** — claimed a send / delivery / completion / count not grounded
  in the trace. The worst outcome; the type-two error.

### Root-cause layer (exactly one)

- **`dispatch-context`** — the engine was starved of the referent before it
  reasoned (Stage 1 `could_resolve=false`). The failure is upstream of the
  engine. THIS is R5's true root: a referential ask dispatched with no
  antecedent.
- **`engine-reasoning`** — the engine HAD the context and still misread, halted
  shallowly, or fabricated.
- **`environment`** — a genuine external wall (process exit, service down,
  unauthenticated dependency) with no held path around it.
- **`permission`** — a missing OS/OAuth grant blocked the held path.
- **`capability-gap`** — the system never gave her the MEANS to serve the intent
  (no tool to deliver a file to the channel; no ambient awareness of what was
  around her). This is OURS — never the user's, never a third party's — and the
  fix is to BUILD the capability, not retrain the engine. A non-delivery she
  "could not" perform because the capability did not exist is `capability-gap`,
  scored as the failure it is (the user got nothing). It is NOT excused as an
  honest blocker: that is reserved for a genuine EXTERNAL wall the user can clear.
- **`none`** — clean fulfillment.

A blocked/off-the-rails arc whose dispatch was context-starved is attributed to
**dispatch-context**, even if a downstream symptom (a calendar permission wall)
is also visible. Attribute to the **most upstream** layer that, if fixed, makes
the rest moot.

### Two carve-outs that are CORRECT, never failures

- **No-op instruction.** When the request explicitly says nothing should be done
  ("just noting this for myself", "don't do anything with this", "ignore that"),
  a brief acknowledgment with NO tool calls IS the fulfillment. Score it
  `FULFILLED`. Performing the action it mentions, against the instruction, is the
  violation.
- **Honest clarification on a genuinely-unresolvable referent.** This is the
  INVERSE of OFF-THE-RAILS. When the request names a referent ("the thing we
  discussed", "finish it") and the antecedent window is genuinely empty or
  insufficient to resolve it, the CORRECT move is a short, specific clarifying
  question that names what was missing — NOT to confabulate a tangent. An honest
  clarification is never `OFF-THE-RAILS` (it did not act on a misread) and never
  `FABRICATED` (it claimed nothing). Score it as serving the user as well as the
  ask allowed (`FULFILLED` when the clarification is the right deliverable). The
  distinction from R5 is the whole point: R5 invented an email/calendar standup
  instead of asking — confabulation is the failure, asking is the fix.

### The honesty rule (a hard ordering constraint)

By overall score: `FULFILLED` > `PARTIAL` > `BLOCKED-UNAVOIDABLE` ≳
`BLOCKED-AVOIDABLE` > `OFF-THE-RAILS` > `FABRICATED`. An honest "I'm blocked,
here's the one fix" MUST outrank a confident wrong answer, and MUST never be
ranked below a smooth fabrication. Never let polish beat honesty. A non-delivery
or non-resolution rooted in a **`capability-gap`** is OUR failure to be able to
serve — the user got nothing — so it ranks with the failures (the OFF-THE-RAILS
band), never with the honest external blockers. "We never built it" is not an
honest blocker; it is a thing to go build.

### `the_one_fix`

One sentence: the single most upstream change that would have made this arc
fulfilled (e.g. "bind the most-recent cursor-watch event into the dispatch so
'the debrief' resolves"). This is what turns the scorecard into an improvement
engine.

---

## Output format

Respond with ONLY valid JSON:

```json
{
  "true_intent": "one sentence",
  "referent": "quoted antecedent or 'none'",
  "could_resolve": true,
  "class": "FULFILLED | PARTIAL | OFF-THE-RAILS | BLOCKED-AVOIDABLE | BLOCKED-UNAVOIDABLE | FABRICATED",
  "root_cause_layer": "dispatch-context | engine-reasoning | environment | permission | capability-gap | none",
  "intent_match": 0.0,
  "completeness": 0.0,
  "effectiveness": 0.0,
  "concision": 0.0,
  "score": 0.0,
  "the_one_fix": "one sentence",
  "reasons": ["one cited string per claim — quote the arc"]
}
```

`score` is the overall fulfillment (1.0 = fully served the true intent, 0.0 =
total failure). Every entry in `reasons` MUST cite specific evidence from the
arc (quote the dispatched task, an antecedent line, a tool call + result, or the
response). Never write a reason without a citation.

## Not evaluated

- Tone or eloquence of the response (except as `concision` penalizes padding).
- Whether the chosen approach was the cheapest among those that serve the true
  intent.
