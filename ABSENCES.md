# Structural absences — what v2 deleted, and why it stays gone

v2 is a from-scratch rebuild. Old Aria is preserved forever at git tag
`aria-v1` / branch `aria-v1-archive`. The following were ripped out on purpose;
re-adding any of them is a regression, not a feature. (Halt-don't-heal: record
the absence so it can't quietly creep back.)

- **Cursor, entirely.** `cursor_bridge`, `cursor_registry`, `cursor_tools`,
  `cursor_external`, `cursor_ide_driver`, the Node `cursor_wrapper/` sidecar,
  the `hooks/` cursor-watch forwarder, every `cursor_*` tool. The one engine is
  Claude Code via the Agent SDK.
- **The non-reasoning front door.** v1's "Gemini routes, does not reason" is the
  defect that sank it (review 1.1). Claude conducts; the voice layer renders.
- **The Discord voice E2EE sidecar** (`discord_voice_bridge/`) and the separate
  `local_voice` path. Phase 1 uses one voice framework (telephony + turn-taking
  + local mic) instead of three fragile components.
- **The premature governance apparatus.** `build_hash` receipts, `live_meter`,
  the `structural_absences.json` checker, and the capture+Gemini-screenshot
  *agreement* harness as a universal code gate (review 1.2/1.3). The
  capture+Gemini primitive returns only at Phase 4 for genuine physical/visual
  state. Verification for code = tests pass + diff exists + builds + one outcome
  log.
- **SpicyLit / Grok**, the Spark control surface, the calibrated judge +
  anchors, tasks/playbooks. They return, if ever, as loops or endpoints — never
  as core.

- **The ephemeral transcript + the per-session telemetry trace** (`src/telemetry.py`,
  `data/traces/*.json`). Both were duplicate, lossy homes for the conversation:
  the transcript died with the process (so Aria never had context across
  sessions) and the trace re-stored the same text only for latency. They are
  collapsed into the ONE durable conversation store (`src/conversation.py`,
  `data/aria.db`), which the conductor loads each turn. Memory is the transcript,
  fed to the model as data (Software 2.0) — never a RAG/vector/summarizer pipeline.

Doctrine note (review 3.6): `.cursor/rules/*.mdc` govern the build-time IDE
agent only; they are inert to Aria's runtime engine. Runtime doctrine reaches
Claude Code via the `{{include:_principles}}` dispatch instruction (see
`src/dispatcher.py`).

## The house (Phase 4) — what must NOT come back

The house is here as the v2 shape promised: ONE substrate (Home Assistant)
behind ONE `home-assistant` endpoint + `loops/home-*.yaml`, and the Spark as a
`spark` endpoint + `loops/local-ask.yaml` (exactly "returns as loops or
endpoints — never as core"). The following stay gone; re-adding any is a
regression:

- **Per-vendor home clients.** No Ring/Hue/Sonos/Lutron client, ever. Every
  device is a Home Assistant integration reached through the one endpoint. HA is
  the home's substrate the way Claude Code is the build engine.
- **The LLM on the home actuation hot path.** The conductor turns speech into
  `(device, action)`; actuation in `src/homeassistant.py` is deterministic
  (a REST `call_service`) and verified against ground truth (a state re-read).
  The model never decides the wire call, and a 200 that didn't change state is
  reported NOT delivered — never a narrated success.
- **A silent cloud/anything fallback when the hub is unreachable.** An
  unconfigured or down hub returns the one-line fix (loud), never a quiet
  substitute, and a connectivity failure is ours to surface — never blamed on HA.
- **The capture+Gemini agreement gate as a universal code gate.** It returns
  only at Phase 4 for genuine physical state (`src/home_verify.py`), exactly as
  this ledger already requires.
