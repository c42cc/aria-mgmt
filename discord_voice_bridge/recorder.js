#!/usr/bin/env node
/**
 * "Echo" — Aria's devoted shadow recorder.
 *
 * A SECOND voice-only Discord application (DISCORD_ECHO_BOT_TOKEN) that joins a
 * voice channel and records what Aria actually plays into it, decrypting the
 * DAVE-E2EE Opus, decoding to PCM, and writing a WAV. This is stage "D" of the
 * voice audibility test: proof that a human voice does (or does not yet) reach
 * the channel — independent of every Python/sidecar telemetry point upstream.
 *
 * It never speaks (selfMute). It reuses the exact receive pipeline as the main
 * sidecar (index.js setupReceiver) plus the same dave_passthrough_patch so it
 * can decrypt the channel's E2EE audio.
 *
 * Usage:
 *   DISCORD_ECHO_BOT_TOKEN=... node recorder.js \
 *     --channel <voice_channel_id> --out /abs/echo.wav --seconds 18 [--target <userId>]
 *
 * stdout is line-delimited JSON so a Python parent can track it:
 *   {"event":"ready","id":"<echo bot id>"}
 *   {"event":"joined","channel_id":"..."}
 *   {"event":"speaker","user_id":"..."}
 *   {"event":"done","path":"...","bytes":N,"ms":M,"speakers":[...]}
 *   {"event":"error","message":"..."}
 *
 * One-time owner step (documented in scripts/voice_audibility_test.py header):
 *   create the app + bot in the Discord Developer Portal, copy its token into
 *   .env as DISCORD_ECHO_BOT_TOKEN, and invite it to the server with the
 *   "Connect" voice permission.
 */

import { Client, GatewayIntentBits, Events } from "discord.js";
import {
  joinVoiceChannel,
  EndBehaviorType,
  VoiceConnectionStatus,
  entersState,
} from "@discordjs/voice";
import prism from "prism-media";
import { writeFileSync } from "node:fs";

// Same DAVE decrypt patch the main sidecar uses — required to decode Aria's
// end-to-end-encrypted Opus in the channel.
import "./dave_passthrough_patch.js";

const RATE = 48000;       // native Discord decode rate
const CHANNELS = 2;       // stereo
const BYTES_PER_SAMPLE = 2;

function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) out[a.slice(2)] = argv[++i];
  }
  return out;
}

const args = parseArgs(process.argv);
const TOKEN = process.env.DISCORD_ECHO_BOT_TOKEN || "";
const CHANNEL_ID = args.channel || "";
const OUT_PATH = args.out || "echo.wav";
const SECONDS = Math.max(2, parseInt(args.seconds || "18", 10));
const TARGET = args.target || ""; // optional: only record this speaker

const emit = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");
const fail = (message) => emit({ event: "error", message });
const diag = (m, extra = {}) =>
  process.stderr.write(`[echo] ${m}${Object.keys(extra).length ? " " + JSON.stringify(extra) : ""}\n`);

if (!TOKEN) { fail("DISCORD_ECHO_BOT_TOKEN is required"); process.exit(1); }
if (!CHANNEL_ID) { fail("--channel is required"); process.exit(1); }

const client = new Client({
  intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates],
});

const chunks = [];          // collected 48k stereo s16le PCM
const speakersSeen = new Set();
const subscribed = new Set();
let totalBytes = 0;

function wavHeader(dataLen) {
  const h = Buffer.alloc(44);
  const byteRate = RATE * CHANNELS * BYTES_PER_SAMPLE;
  const blockAlign = CHANNELS * BYTES_PER_SAMPLE;
  h.write("RIFF", 0);
  h.writeUInt32LE(36 + dataLen, 4);
  h.write("WAVE", 8);
  h.write("fmt ", 12);
  h.writeUInt32LE(16, 16);           // PCM fmt chunk size
  h.writeUInt16LE(1, 20);            // audio format = PCM
  h.writeUInt16LE(CHANNELS, 22);
  h.writeUInt32LE(RATE, 24);
  h.writeUInt32LE(byteRate, 28);
  h.writeUInt16LE(blockAlign, 32);
  h.writeUInt16LE(BYTES_PER_SAMPLE * 8, 34);
  h.write("data", 36);
  h.writeUInt32LE(dataLen, 40);
  return h;
}

function writeWavAndExit() {
  const data = Buffer.concat(chunks, totalBytes);
  const buf = Buffer.concat([wavHeader(data.length), data]);
  try {
    writeFileSync(OUT_PATH, buf);
    emit({
      event: "done",
      path: OUT_PATH,
      bytes: data.length,
      ms: SECONDS * 1000,
      seconds: +(data.length / (RATE * CHANNELS * BYTES_PER_SAMPLE)).toFixed(2),
      speakers: [...speakersSeen],
    });
  } catch (e) {
    fail(`write wav: ${e.message}`);
  }
  setTimeout(() => process.exit(0), 100);
}

function subscribeSpeaker(receiver, userId) {
  if (TARGET && userId !== TARGET) return;
  if (subscribed.has(userId)) return;
  subscribed.add(userId);
  speakersSeen.add(userId);
  emit({ event: "speaker", user_id: userId });
  diag("subscribing", { userId });

  const opus = receiver.subscribe(userId, {
    end: { behavior: EndBehaviorType.AfterSilence, duration: 1000 },
  });
  const decoder = new prism.opus.Decoder({ frameSize: 960, channels: CHANNELS, rate: RATE });
  opus.pipe(decoder);

  decoder.on("data", (pcm) => { chunks.push(pcm); totalBytes += pcm.length; });
  const cleanup = () => { subscribed.delete(userId); };
  opus.on("end", cleanup);
  opus.on("close", cleanup);
  opus.on("error", (e) => { diag("opus stream error", { message: e.message }); cleanup(); });
  decoder.on("error", (e) => diag("decoder error", { message: e.message }));
}

async function joinAndRecord() {
  const channel = await client.channels.fetch(CHANNEL_ID);
  if (!channel || !channel.guild) { fail(`channel not found: ${CHANNEL_ID}`); process.exit(1); }

  const conn = joinVoiceChannel({
    channelId: channel.id,
    guildId: channel.guild.id,
    adapterCreator: channel.guild.voiceAdapterCreator,
    selfDeaf: false,
    selfMute: true,
    debug: false,
  });
  conn.on("error", (e) => diag("voice connection error", { message: e.message }));

  try {
    await entersState(conn, VoiceConnectionStatus.Ready, 20000);
  } catch (e) {
    fail(`echo voice connect failed: ${e.message}`);
    try { conn.destroy(); } catch {}
    process.exit(1);
  }
  emit({ event: "joined", channel_id: CHANNEL_ID });
  diag("recording", { seconds: SECONDS, target: TARGET || "(any)" });

  const receiver = conn.receiver;
  receiver.speaking.on("start", (userId) => subscribeSpeaker(receiver, userId));

  setTimeout(() => {
    try { conn.destroy(); } catch {}
    writeWavAndExit();
  }, SECONDS * 1000);
}

client.once(Events.ClientReady, async () => {
  emit({ event: "ready", id: client.user?.id, tag: client.user?.tag });
  try { await joinAndRecord(); }
  catch (e) { fail(`join/record: ${e.message}`); process.exit(1); }
});

client.on(Events.Error, (e) => diag("client error", { message: e.message }));

try {
  await client.login(TOKEN);
} catch (e) {
  fail(`login failed: ${e.message}`);
  process.exit(1);
}
