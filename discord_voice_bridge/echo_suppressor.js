/**
 * Reference-gated echo suppressor — the voice "floor" primitive.
 *
 * Root cause it kills (forensic 2026-06-25): Aria's spoken audio, played into
 * the channel, is re-captured by the open mic and forwarded back to Gemini Live
 * as if the user said it. Gemini answers its own echo and the call collapses
 * into a 16-turn self-loop (conversation_log 643..658). The prior "fix" was a
 * printed line telling the user to wear headphones (local_voice.py) — prose,
 * not a primitive.
 *
 * The primitive: the bridge already HOLDS exactly what Aria is playing, so
 * before forwarding an incoming frame it SENSES whether that frame is a delayed
 * copy of Aria's own recent playback. This is "AEC at the bridge" in its robust,
 * dependency-free form — an envelope cross-correlation against the playback
 * reference, not adaptive sample cancellation (which is unstable over Discord's
 * jittered network path and needs a native lib we do not ship).
 *
 * Why it preserves barge-in: a real interruption is INDEPENDENT of Aria's
 * playback, so its energy envelope does not correlate with the reference and it
 * is forwarded. Only audio that tracks what Aria is currently saying is
 * suppressed. When Aria is silent, everything is forwarded unconditionally.
 *
 * Failure posture (halt-don't-heal): it never silently eats audio. Every
 * suppression is counted and surfaced; on any uncertainty (too little
 * reference, near-zero variance) it FORWARDS — a missed echo is recoverable,
 * a swallowed user turn is not.
 *
 * Pure and deterministic: no Discord, no I/O — unit-tested in
 * test_echo_suppressor.js with synthetic signals.
 */

const IN_SR = 16000; // incoming mic PCM rate (s16le mono)
const REF_SR = 24000; // playback reference rate (s16le mono, Gemini -> Discord)
const HOP_MS = 8; // envelope frame = 8ms, computed on both rates -> same grid

function rmsEnvelope(buf, sampleRate) {
  // One RMS value per HOP_MS window. `buf` is a Buffer of s16le mono.
  const hop = Math.round((sampleRate * HOP_MS) / 1000);
  const n = Math.floor(buf.length / 2);
  const out = [];
  for (let i = 0; i + hop <= n; i += hop) {
    let acc = 0;
    for (let j = 0; j < hop; j++) {
      const s = buf.readInt16LE((i + j) * 2);
      acc += s * s;
    }
    out.push(Math.sqrt(acc / hop));
  }
  return out;
}

// Max normalized (Pearson) cross-correlation of `a` against any same-length
// window of `ref`, searching lags 0..maxLag. Returns 0 when undefined (flat
// signal / not enough reference) so "uncertain" never reads as "echo".
function bestCorrelation(a, ref, maxLag) {
  const m = a.length;
  if (m < 2 || ref.length < m) return 0;
  const meanA = a.reduce((x, y) => x + y, 0) / m;
  let varA = 0;
  for (const v of a) varA += (v - meanA) * (v - meanA);
  if (varA <= 1e-9) return 0;
  let best = 0;
  const maxStart = ref.length - m;
  const lo = Math.max(0, maxStart - maxLag);
  for (let start = maxStart; start >= lo; start--) {
    let meanR = 0;
    for (let k = 0; k < m; k++) meanR += ref[start + k];
    meanR /= m;
    let cov = 0;
    let varR = 0;
    for (let k = 0; k < m; k++) {
      const dr = ref[start + k] - meanR;
      cov += (a[k] - meanA) * dr;
      varR += dr * dr;
    }
    if (varR <= 1e-9) continue;
    const corr = cov / Math.sqrt(varA * varR);
    if (corr > best) best = corr;
  }
  return best;
}

export class EchoSuppressor {
  constructor(opts = {}) {
    this.enabled = opts.enabled !== false;
    this.corrThreshold = opts.corrThreshold ?? 0.6;
    this.hangoverMs = opts.hangoverMs ?? 300; // Aria "in the room" this long after last ref
    const maxLagMs = opts.maxLagMs ?? 700; // search echo delay up to here
    this.maxLagFrames = Math.ceil(maxLagMs / HOP_MS);
    this.refEnvMax = opts.refEnvMax ?? Math.ceil(2000 / HOP_MS); // ~2s history
    this._refEnv = [];
    this._lastRefMs = 0;
    // Observable counters — never a silent drop.
    this.suppressed = 0;
    this.forwardedBargeIn = 0;
    this.forwardedSilent = 0;
  }

  /** Register reference PCM (24kHz mono s16le) Aria is about to play. */
  pushReference(buf, nowMs) {
    if (!this.enabled || !buf || buf.length < 2) return;
    const env = rmsEnvelope(buf, REF_SR);
    if (env.length) {
      for (const e of env) this._refEnv.push(e);
      if (this._refEnv.length > this.refEnvMax) {
        this._refEnv.splice(0, this._refEnv.length - this.refEnvMax);
      }
      this._lastRefMs = nowMs;
    }
  }

  /**
   * Decide whether an incoming mic frame (16kHz mono s16le) should be forwarded
   * to Gemini. Returns {forward, reason, corr}.
   */
  classify(buf, nowMs) {
    if (!this.enabled) return { forward: true, reason: "disabled", corr: 0 };
    const ariaSpeaking = nowMs - this._lastRefMs <= this.hangoverMs;
    if (!ariaSpeaking) {
      this.forwardedSilent++;
      return { forward: true, reason: "aria_silent", corr: 0 };
    }
    const inEnv = rmsEnvelope(buf, IN_SR);
    const corr = bestCorrelation(inEnv, this._refEnv, this.maxLagFrames);
    if (corr >= this.corrThreshold) {
      this.suppressed++;
      return { forward: false, reason: "echo", corr };
    }
    this.forwardedBargeIn++;
    return { forward: true, reason: "barge_in", corr };
  }
}

// Exported for unit testing.
export const _internal = { rmsEnvelope, bestCorrelation, HOP_MS };
