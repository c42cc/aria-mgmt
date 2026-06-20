# Engineering principles — the doctrine that governs every plan, build, and verification

These are not suggestions. A change that violates one is a regression, not a feature.
This is the single home of the doctrine; every reasoning and build persona references
it (it is not pasted into them), so it cannot drift.

- **Operate on the dysfunctional primitive, never the symptom.** When something is
  broken, find the root primitive and collapse it. Never add a layer that hides the
  failure — no retry to paper over a wall, no fallback default feeding a decision, no
  reconciler whose job is to make two copies agree. If two copies must agree, find them
  and collapse them into one.
- **Fewest moving parts that reach the natural state of the system.** Prefer the
  simplest, most fundamental solution. Every capability should be an operation on a
  primitive that already exists, not a new subsystem, broker, queue, or second loop.
- **Reuse over rebuild.** Search for an existing home before adding a module. Extend what
  is there. Do not duplicate a fact, a loop, or a store.
- **No legacy, no remnants.** We do not support back-compat, style variants, or bloat.
  When a change makes something gone, record it so it stays gone — never leave a dead
  path "just in case." Clean up after yourself.
- **All failures are observable.** No silent fallbacks. No `try/except: pass`. A broken
  mechanism is a loud, typed error — never a quiet degraded result, never a fabricated
  success. Never blame a third party: a timeout, a thundering herd, a delay is ours to
  surface and fix at the root.
- **Halt, do not heal, on missing data.** Missing data, an untagged action, or a broken
  dependency → stop and report exactly what broke and the one-command fix. Do not heal
  past it.
- **State what you checked and what you did not.** No implicit "all clear." Name your
  evidence. "Unverified" counts as a failure, not a pass.
- **Done means verified close to the end user — and you watched it go green.** A change
  is done only when a real request completes and a calibrated check marks it correct. A
  green meter, a passing unit test, or a rebuilt branch is not done.
