# Phase 4 — the house

Aria controls the physical house the same way she does everything else in v2:
**capability is data (loops) + one endpoint.** No per-vendor clients, no v1 MCP
core — the whole house is one substrate (Home Assistant) behind one dispatcher
endpoint, and each home capability is a `loops/*.yaml` file the conductor already
knows how to drive.

## The shape

- **One substrate: Home Assistant.** Every device (lights, switches, locks,
  covers/blinds, media, climate, cameras, scenes) is an HA `entity` with a
  `state` and `services`. Aria learns ONE verb surface — `call_service` +
  `get_state` — not N device APIs. Buy local-first, standards-based gear
  (Matter/Zigbee/Thread/Z-Wave; ONVIF/RTSP/PoE cameras) so the house works
  without a vendor cloud.
- **One endpoint: `home-assistant`** (`src/dispatcher.py::_run_home`, logic in
  `src/homeassistant.py`). The conductor (Claude) turns speech into
  `(device, action, value)`; the endpoint actuates HA's REST API
  **deterministically** and verifies the result against **ground truth** by
  re-reading the entity's state — never the model's narration. The LLM is never
  on the actuation hot path.
- **Loops (capability as data):** `loops/home-control.yaml` (on/off, lock/unlock,
  open/close, set a level, activate a scene) and `loops/home-status.yaml` (read
  state). Adding a capability is adding a file; the core never grows. on/off
  generalize across domains via `homeassistant.turn_on/off`, so a new device
  *type* usually needs nothing at all.
- **Safety = the existing mechanical go-gate.** A DISPATCH only fires after a
  CONFIRM for that exact loop + an explicit go (`src/brain.py::ready_to_dispatch`).
  Unlocking a door, opening the garage, or disarming an alarm therefore confirms
  for free. HA's own entity-exposure is an independent second gate: Aria can only
  touch what you exposed.
- **The Spark as an endpoint too:** `loops/local-ask.yaml` + the `spark` endpoint
  run plain text tasks on a local open-source model served on the DGX Spark (vLLM
  serves the Anthropic Messages API natively, so the SDK drives it via base_url).
  This honors ABSENCES.md — the Spark returns "as an endpoint, never as core."

## Setup

1. Stand up Home Assistant on a dedicated, always-on host (HA Green or a
   mini-PC/Pi with HA OS), local-first, reachable over Tailscale. Do NOT
   co-locate it on the Spark — the house must not reboot when the AI box does.
2. Create a long-lived access token; in HA, **expose only** the entities Aria
   should control (Settings > Voice assistants > Expose).
3. Set `HASS_URL` + `HASS_TOKEN` in `.env`. (`SPARK_BASE_URL` for the Spark loop.)
4. `make run` and say "turn on the living room lights" / "is the front door
   locked?".

Unconfigured is honest, not silent: the endpoint returns the one-line fix and
never falls back to anything.

## Verified-done (two proofs for a body that moves atoms)

1. **State (always):** the dispatcher re-reads the entity after the call; "done"
   means the entity actually reports the requested state. A 200 that didn't change
   anything is reported as NOT delivered (a real test covers this).
2. **Physical (Phase 4, on demand):** `python -m src.home_verify --camera
   camera.garage --question "Is the garage door fully closed?"` pulls a real
   camera frame and asks Gemini independently. This is the one place ABSENCES.md
   permits the capture+Gemini primitive — genuine physical/visual state.

Tests: `tests/test_home.py` (entity resolution, the action map, and full
`dispatcher.run` round-trips against an in-process HA API double over a real
socket, including ground-truth catching a no-op) and `tests/test_spark_endpoint.py`.

## Where this sits in the roadmap

This landed ahead of Phase 3's full trust boundary (a deliberate call). Until
Phase 3 lands per-endpoint scoping, the dangerous-action protection IS the
mechanical go-gate's CONFIRM step plus HA's exposure whitelist. The deterministic
actuation, ground-truth verification, and honest-when-unconfigured posture do not
depend on Phase 3.

## Scaling by addition

- A new device of an existing type: expose it in HA — nothing in Aria changes.
- A genuinely new action class (e.g., `vacuum.start`): one row in
  `src/homeassistant.py::plan_call` (+ its domain in `_DOMAINS_FOR_VERB`).
- A new execution substrate: a new endpoint in `src/dispatcher.py` + loops.
