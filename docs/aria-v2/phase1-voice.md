# Phase 1 — the phone call (LiveKit)

The architecture: LiveKit owns what must be fast and is genuinely hard
(telephony/SIP, audio transport, semantic turn detection, barge-in, VAD). Aria's
brain is unchanged — `src/voice.py::ConductorLLM` is a LiveKit `llm.LLM` whose
every completion is one `AriaBrain` turn, so **Claude conducts and the voice
layer only renders** (review 1.1). A confirmed build runs in the background and
reports via the loop's channel, so a multi-second engine run never freezes the
call (review 2.2).

## What is verified vs. what you verify

- **Verified by me (no audio):** the brain bridge — `ConductorLLM` driven through
  a real LiveKit `ChatContext` produces Aria's turn via the conductor and records
  it; the go-gate holds in `AriaBrain` (unit tests in `tests/test_brain.py`).
- **You verify (an agent can't speak):** the audio loop and the phone call, after
  provisioning below. This is the honest boundary — I will not claim a working
  call I didn't hear.

## The wall (the one capability needed to get past it)

There are no `LIVEKIT_*` keys or a phone number in `.env`. A real call needs a
**LiveKit Cloud account** (free tier covers local + Inference) and, for the
actual phone line, a **number** (LiveKit Phone Numbers, or a SIP trunk).

## Provision + test (in order)

1. `pip install -e '.[voice]'` (adds `livekit-agents` + the silero VAD plugin).
2. Create a LiveKit Cloud project, then add to `.env`:
   `LIVEKIT_URL=`, `LIVEKIT_API_KEY=`, `LIVEKIT_API_SECRET=`.
3. **Talk to her locally (no phone, no extra accounts):**
   `python -m src.voice console` — speak into your Mac mic; Ctrl+B toggles
   text/audio, Q quits. This verifies the full voice loop end to end.
4. **The phone call:** in the LiveKit dashboard, Telephony → Phone numbers → buy a
   US number, then attach a dispatch rule routing the number to this agent
   (`agentName` matching the deployed agent). Deploy with `python -m src.voice
   start` (or LiveKit Cloud), then call the number.
   (Bring-your-own-number works too via a SIP trunk — Telnyx/Twilio/Plivo — see
   LiveKit's SIP trunk docs.)

## STT/TTS

Defaults use LiveKit Inference (Deepgram STT, Cartesia TTS — needs the LiveKit
Cloud key). To keep audio fully on your own keys, swap `inference.STT/TTS` in
`_build_server()` for plugin classes with provider keys.

## The latency primitive (partly built; finish at the console loop)

Above ~800ms, voice feels dead. **Tiering is implemented** (`ARIA_CONDUCTOR_TIER`,
default on): routine/interview turns run on the fast tier (`claude-haiku-4-5`,
verified to route across all loops and hold the guards), and the nuanced
post-build REPORT stays on Opus. That dropped routine turns from ~3s to ~1.6s.

The remaining levers to reach sub-800ms — best built against the live console
loop, where audio latency is real — are: stream the conductor's tokens to TTS
(don't wait for the full turn), a spoken filler while a turn is forming, and a
leaner system prompt. LiveKit handles the rest (turn detection, barge-in).
