/**
 * Unit proof of the echo-suppressor floor primitive. No Discord, no network —
 * synthetic PCM only. Run: `node test_echo_suppressor.js` (exit 0 = green).
 *
 * Proves the three states that matter:
 *   1. Aria silent            -> forward (normal listening).
 *   2. Aria speaking + echo    -> SUPPRESS (the 643..658 self-loop is killed).
 *   3. Aria speaking + barge-in -> forward (interruption survives — the guard).
 */
import { EchoSuppressor } from "./echo_suppressor.js";

let failures = 0;
function check(name, cond) {
  if (cond) {
    console.log(`  ok  - ${name}`);
  } else {
    console.log(`  FAIL- ${name}`);
    failures++;
  }
}

// --- synthetic signal helpers (s16le mono Buffers) -------------------------
function pcm(sampleRate, ms, fn) {
  const n = Math.round((sampleRate * ms) / 1000);
  const b = Buffer.alloc(n * 2);
  for (let i = 0; i < n; i++) {
    let s = Math.round(fn(i / sampleRate));
    s = Math.max(-32768, Math.min(32767, s));
    b.writeInt16LE(s, i * 2);
  }
  return b;
}
const silence = (sampleRate, ms) => pcm(sampleRate, ms, () => 0);
// A warbling, amplitude-modulated tone gives a non-flat envelope so correlation
// is meaningful (a pure constant-amplitude tone has a flat envelope).
const speech = (sampleRate, ms, f, mod, amp = 9000) =>
  pcm(sampleRate, ms, (t) => amp * (0.5 + 0.5 * Math.sin(2 * Math.PI * mod * t)) * Math.sin(2 * Math.PI * f * t));

// Resample-free "echo" of a reference: the SAME modulation envelope captured by
// the mic at 16k, attenuated and a little noisy — what the room returns.
const echoOf = (ms, mod) => pcm(16000, ms, (t) =>
  3000 * (0.5 + 0.5 * Math.sin(2 * Math.PI * mod * t)) * Math.sin(2 * Math.PI * 500 * t)
  + 200 * Math.sin(2 * Math.PI * 1234 * t));

// Independent human-like speech: an IRREGULAR (non-periodic) amplitude envelope
// from a deterministic PRNG — what real barge-in looks like. It does not track
// Aria's smooth playback envelope, so it must NOT be mistaken for echo.
function bargeIn(ms) {
  let seed = 1337;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  const sr = 16000;
  const n = Math.round((sr * ms) / 1000);
  const b = Buffer.alloc(n * 2);
  let amp = 0.5;
  for (let i = 0; i < n; i++) {
    if (i % Math.round(sr * 0.013) === 0) amp = 0.15 + 0.85 * rnd(); // ~13ms syllabic jumps
    let s = 9000 * amp * Math.sin(2 * Math.PI * 240 * (i / sr));
    s = Math.max(-32768, Math.min(32767, Math.round(s)));
    b.writeInt16LE(s, i * 2);
  }
  return b;
}

// --- 1. Aria silent: always forward ---------------------------------------
{
  const es = new EchoSuppressor();
  const v = es.classify(speech(16000, 200, 300, 5), 10_000);
  check("aria silent -> forward", v.forward && v.reason === "aria_silent");
}

// --- 2. Aria speaking + echo: suppress ------------------------------------
{
  const es = new EchoSuppressor();
  const now = 50_000;
  // Aria plays a modulated tone (24k reference).
  es.pushReference(speech(24000, 600, 500, 3), now);
  // The room returns the same modulation envelope a beat later.
  const v = es.classify(echoOf(120, 3), now + 80);
  check("aria speaking + echo -> SUPPRESS", !v.forward && v.reason === "echo");
  check("echo suppression counted (not silent)", es.suppressed === 1);
}

// --- 3. Aria speaking + barge-in: forward ---------------------------------
{
  const es = new EchoSuppressor();
  const now = 70_000;
  es.pushReference(speech(24000, 600, 500, 3), now);
  // Independent user speech: an irregular (non-periodic) envelope -> decorrelated.
  const v = es.classify(bargeIn(250), now + 80);
  check("aria speaking + barge-in -> forward", v.forward && v.reason === "barge_in");
  check("barge-in counted", es.forwardedBargeIn === 1);
}

// --- 4. disabled = pure passthrough ---------------------------------------
{
  const es = new EchoSuppressor({ enabled: false });
  es.pushReference(speech(24000, 200, 500, 3), 90_000);
  const v = es.classify(echoOf(120, 3), 90_010);
  check("disabled -> forward (passthrough)", v.forward && v.reason === "disabled");
}

// --- 5. hangover expiry: echo after Aria stops is just late audio ---------
{
  const es = new EchoSuppressor({ hangoverMs: 200 });
  es.pushReference(speech(24000, 200, 500, 3), 100_000);
  const v = es.classify(echoOf(120, 3), 100_500); // 500ms later, past hangover
  check("past hangover -> forward (aria silent)", v.forward && v.reason === "aria_silent");
}

if (failures) {
  console.log(`\nECHO-SUPPRESSOR: RED (${failures} failed)`);
  process.exit(1);
}
console.log("\nECHO-SUPPRESSOR: GREEN");
