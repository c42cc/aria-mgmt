#!/usr/bin/env node
/**
 * Playback pipeline regression test.
 *
 * Reproduces the bug reported on 2026-05-13:
 *   "the first request or command that I have results in a double response.
 *    Then I can't talk to aria anymore."
 *
 * Root cause: the bridge used a single persistent AudioResource. After the
 * first audio burst, @discordjs/voice's AudioPlayer called stop() on the
 * resource (because resource.read() returned null for maxMissedFrames
 * cycles). The resource entered silence-padding mode and died. Subsequent
 * pushes to the still-existing playbackStream landed in a dead consumer.
 * Production symptom: `voice bridge error: upsampler: Premature close`.
 *
 * The fix in index.js rebuilds playbackStream + FFmpeg + AudioResource on
 * each new burst, triggered by the player's Idle event.
 *
 * This test mirrors the production playback pipeline (one shared
 * AudioPlayer for the lifetime of the connection, fresh stream/upsampler/
 * resource per burst) and asserts that three sequential bursts each
 * produce audio frames and each emit a clean Idle event. With the buggy
 * "one resource forever" architecture, bursts 2 and 3 produce zero frames.
 *
 * Exits 0 on PASS, non-zero on FAIL.
 */

import {
  createAudioPlayer,
  createAudioResource,
  StreamType,
  AudioPlayerStatus,
  NoSubscriberBehavior,
} from "@discordjs/voice";
import prism from "prism-media";
import { Readable } from "node:stream";

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// Build N ms of 24kHz mono s16le sine-wave PCM at the given frequency.
function makeBurstPcm(ms, freqHz) {
  const sampleRate = 24000;
  const totalSamples = Math.floor((sampleRate * ms) / 1000);
  const buf = Buffer.alloc(totalSamples * 2);
  for (let i = 0; i < totalSamples; i++) {
    const v = Math.floor(Math.sin((2 * Math.PI * freqHz * i) / sampleRate) * 16000);
    buf.writeInt16LE(v, i * 2);
  }
  return buf;
}

async function testPlaybackRecoversFromIdle() {
  const failures = [];
  const burstFrames = [];
  const idleEvents = [];
  const errorEvents = [];
  let currentBurst = 0;
  let dispatchedThisBurst = 0;

  let player = null;
  let playbackStream = null;

  function teardownPlaybackStream() {
    if (!playbackStream) return;
    try { playbackStream.push(null); } catch {}
    try { playbackStream.destroy(); } catch {}
    playbackStream = null;
  }

  function ensurePlaybackStream() {
    if (playbackStream) return playbackStream;
    if (!player) return null;
    const stream = new Readable({ read() {} });
    const upsampler = new prism.FFmpeg({
      args: [
        "-analyzeduration", "0",
        "-loglevel", "0",
        "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "-",
        "-ar", "48000", "-ac", "2", "-f", "s16le",
      ],
    });
    upsampler.on("error", (e) => {
      // "Premature close" is expected between bursts (the resource detached
      // before FFmpeg's stdout fully drained). Anything else is a real bug.
      if (!/Premature close/i.test(e.message)) errorEvents.push(`upsampler: ${e.message}`);
    });
    stream.pipe(upsampler);
    const resource = createAudioResource(upsampler, { inputType: StreamType.Raw });
    player.play(resource);
    playbackStream = stream;
    return playbackStream;
  }

  function doPlay(buffer) {
    if (!player) return;
    const stream = ensurePlaybackStream();
    if (!stream) return;
    stream.push(buffer);
  }

  // Mock connection: pretends to be Ready and counts non-silence packets
  // per burst. Enough for the global audio cycle to actually run and read
  // the resource at 50fps.
  const SILENCE = Buffer.from([0xf8, 0xff, 0xfe]);
  const fakeConnection = {
    state: { status: "ready", connectionData: { speaking: false } },
    prepareAudioPacket(packet) {
      if (packet && !packet.equals(SILENCE)) dispatchedThisBurst++;
      return packet;
    },
    dispatchAudio() {},
    setSpeaking() {},
  };

  player = createAudioPlayer({
    behaviors: { noSubscriber: NoSubscriberBehavior.Play },
  });
  player.on(AudioPlayerStatus.Idle, () => {
    idleEvents.push(`burst ${currentBurst}`);
    teardownPlaybackStream();
  });
  player.on("error", (e) => {
    errorEvents.push(`player: ${e.message}`);
    teardownPlaybackStream();
  });

  player.subscribe(fakeConnection);

  // Three bursts with idle gaps. The buggy "one resource forever" code
  // produced [50, 0, 0] and 1 Idle event; the fix produces [50, 50, 50]
  // and 3 Idle events.
  for (const [i, pcm] of [
    [1, makeBurstPcm(1000, 440)],
    [2, makeBurstPcm(1000, 660)],
    [3, makeBurstPcm(1000, 880)],
  ]) {
    currentBurst = i;
    dispatchedThisBurst = 0;
    doPlay(pcm);
    // Signal end-of-input so FFmpeg flushes immediately. In production
    // Gemini streams continuously and FFmpeg flushes when its buffer fills;
    // in the test we don't have continuous input so we close stdin.
    if (playbackStream) {
      try { playbackStream.push(null); } catch {}
    }
    // Wait for the player to consume the whole burst and go Idle.
    await sleep(2200);
    burstFrames.push(dispatchedThisBurst);
  }

  try { player.stop(true); } catch {}

  // 1s of audio at 50fps → ~50 packets. Require at least 30 to allow
  // FFmpeg startup slack.
  const MIN_PACKETS = 30;
  burstFrames.forEach((n, i) => {
    if (n < MIN_PACKETS) {
      failures.push(`burst ${i + 1}: only ${n} packets (need >= ${MIN_PACKETS})`);
    }
  });
  if (idleEvents.length < 3) {
    failures.push(`expected 3 Idle events, got ${idleEvents.length}: ${idleEvents.join(", ")}`);
  }
  if (errorEvents.length > 0) {
    failures.push(`unexpected errors: ${errorEvents.join("; ")}`);
  }

  return { failures, detail: { burstFrames, idleEvents, errorEvents } };
}

const result = await testPlaybackRecoversFromIdle();
if (result.failures.length === 0) {
  console.log(
    `PASS: playback_recovers_from_idle ` +
    `(frames=${result.detail.burstFrames.join(",")}, idles=${result.detail.idleEvents.length})`
  );
  process.exit(0);
} else {
  console.error("FAIL: playback_recovers_from_idle");
  result.failures.forEach((f) => console.error(`  - ${f}`));
  console.error("Detail:", JSON.stringify(result.detail, null, 2));
  process.exit(1);
}
