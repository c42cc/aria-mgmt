#!/usr/bin/env node
/**
 * Discord voice sidecar. Owns the voice WebSocket and nothing else.
 *
 * Logs in as a SECOND Discord application (DISCORD_VOICE_BOT_TOKEN).
 * The existing py-cord bot keeps doing text/commands/threads. The two bots
 * never talk through Discord; they are glued by the Python parent process.
 *
 * Protocol: line-delimited JSON on stdio.
 *
 *   stdin (commands from Python):
 *     {"action": "join",  "channel_id": "..."}
 *     {"action": "leave"}
 *     {"action": "play",  "pcm_b64": "..."}     // 24kHz mono PCM s16le (Gemini -> Discord)
 *     {"action": "shutdown"}
 *
 *   stdout (events to Python):
 *     {"event": "ready"}                        // Discord login complete
 *     {"event": "joined", "channel_id": "..."}  // voice connect succeeded
 *     {"event": "left"}                         // voice connect destroyed
 *     {"event": "audio",  "pcm_b64": "...", "user_id": "..."}  // 16kHz mono PCM s16le
 *     {"event": "error",  "message": "..."}
 *
 * Audio is filtered Node-side: only frames from AUTHORIZED_VOICE_USER_ID
 * are forwarded to Python.
 */

import { Client, GatewayIntentBits, Events } from "discord.js";
import {
  joinVoiceChannel,
  createAudioPlayer,
  createAudioResource,
  StreamType,
  EndBehaviorType,
  VoiceConnectionStatus,
  entersState,
} from "@discordjs/voice";
import prism from "prism-media";
import { Readable } from "node:stream";
import readline from "node:readline";

const BOT_TOKEN = process.env.DISCORD_VOICE_BOT_TOKEN || "";
const AUTHORIZED_USER_ID = process.env.AUTHORIZED_VOICE_USER_ID || "";

if (!BOT_TOKEN) {
  process.stderr.write("DISCORD_VOICE_BOT_TOKEN is required\n");
  process.exit(1);
}

const emit = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");
const fail = (message) => emit({ event: "error", message });
const diag = (message, extra = {}) =>
  process.stderr.write(`[voice-bridge] ${message}${Object.keys(extra).length ? " " + JSON.stringify(extra) : ""}\n`);

const client = new Client({
  intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates],
});

let connection = null;
let player = null;
let playbackStream = null;
const speakerSubscriptions = new Set();

client.once(Events.ClientReady, () => {
  diag("ClientReady", { user: client.user?.tag, id: client.user?.id });
  emit({ event: "ready" });
});

client.on(Events.Error, (e) => diag("client error", { message: e.message }));
client.on(Events.ShardDisconnect, (ev, id) =>
  diag("shard disconnect", { id, code: ev?.code, reason: ev?.reason })
);
client.on(Events.ShardReconnecting, (id) => diag("shard reconnecting", { id }));

function attachConnectionDiagnostics(conn, channel_id) {
  conn.on("stateChange", (oldState, newState) => {
    const extra = { from: oldState.status, to: newState.status };
    if (newState.reason) extra.reason = newState.reason;
    if (newState.closeCode) extra.closeCode = newState.closeCode;
    diag("voice state change", extra);
  });

  conn.on(VoiceConnectionStatus.Disconnected, async () => {
    diag("voice Disconnected — attempting recovery");
    try {
      await Promise.race([
        entersState(conn, VoiceConnectionStatus.Signalling, 5_000),
        entersState(conn, VoiceConnectionStatus.Connecting, 5_000),
      ]);
      diag("voice recovery in progress (reconnecting)");
    } catch {
      diag("voice recovery failed — destroying connection");
      try { conn.destroy(); } catch {}
      if (connection === conn) connection = null;
      fail("voice connection lost and could not recover");
    }
  });

  conn.on("error", (e) => {
    diag("voice connection error", { message: e.message });
    fail(`voice connection error: ${e.message}`);
  });
}

async function attemptVoiceConnect(channel, attempt) {
  diag("joinVoiceChannel attempt", {
    attempt,
    channel_id: channel.id,
    guild_id: channel.guild.id,
    channel_name: channel.name,
    member_count: channel.members?.size,
  });

  const conn = joinVoiceChannel({
    channelId: channel.id,
    guildId: channel.guild.id,
    adapterCreator: channel.guild.voiceAdapterCreator,
    selfDeaf: false,
    selfMute: false,
    debug: true,
    // DAVE: when bot joins a channel where a user is already speaking, the
    // welcome from Discord can be at a stale MLS epoch. Discord-side default
    // is 36 consecutive decrypt failures before recovery (re-request fresh
    // commit/welcome via DaveMlsInvalidCommitWelcome), which is much longer
    // than a typical short utterance — the failure counter resets when the
    // stream ends from AfterSilence, so the bot never reaches the threshold
    // during natural speech. Lower this so the bot self-heals fast.
    decryptionFailureTolerance: 5,
  });
  conn.on("debug", (msg) => diag("voice debug", { msg: String(msg).slice(0, 300) }));
  attachConnectionDiagnostics(conn, channel.id);

  try {
    await entersState(conn, VoiceConnectionStatus.Ready, 20_000);
    diag("voice Ready", { channel_id: channel.id, attempt });
    return conn;
  } catch (e) {
    const lastState = conn?.state?.status;
    diag("voice connect attempt failed", {
      attempt,
      message: e.message,
      lastState,
    });
    try { conn.destroy(); } catch {}
    throw e;
  }
}

async function doJoin({ channel_id }) {
  if (connection) {
    diag("destroying existing connection before new join");
    try { connection.destroy(); } catch {}
    connection = null;
  }
  speakerSubscriptions.clear();

  const channel = await client.channels.fetch(channel_id);
  if (!channel || !channel.guild) {
    fail(`channel not found or not a guild channel: ${channel_id}`);
    return;
  }

  const maxAttempts = 3;
  let lastError = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      connection = await attemptVoiceConnect(channel, attempt);
      lastError = null;
      break;
    } catch (e) {
      lastError = e;
      connection = null;
      if (attempt < maxAttempts) {
        const backoffMs = 1500 * attempt;
        diag("waiting before retry", { attempt, backoffMs });
        await new Promise((r) => setTimeout(r, backoffMs));
      }
    }
  }

  if (!connection) {
    fail(`voice connect failed after ${maxAttempts} attempts: ${lastError?.message ?? "unknown"}`);
    return;
  }

  setupReceiver(connection);
  setupPlayback(connection);

  emit({ event: "joined", channel_id });
}

function setupReceiver(conn) {
  const receiver = conn.receiver;

  receiver.speaking.on("start", (userId) => {
    if (AUTHORIZED_USER_ID && userId !== AUTHORIZED_USER_ID) {
      diag("audio frame ignored — userId not authorized", {
        speaker: userId,
        expected: AUTHORIZED_USER_ID,
      });
      return;
    }
    if (speakerSubscriptions.has(userId)) return;
    speakerSubscriptions.add(userId);

    diag("subscribing to speaker", { userId });
    const opusStream = receiver.subscribe(userId, {
      end: { behavior: EndBehaviorType.AfterSilence, duration: 500 },
    });
    const decoder = new prism.opus.Decoder({ frameSize: 960, channels: 2, rate: 48000 });
    const downsampler = new prism.FFmpeg({
      args: [
        "-analyzeduration", "0",
        "-loglevel", "0",
        "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "-",
        "-ar", "16000", "-ac", "1", "-f", "s16le",
      ],
    });

    opusStream.pipe(decoder).pipe(downsampler);

    let frameCount = 0;
    downsampler.on("data", (chunk) => {
      if (frameCount === 0) diag("first audio frame from speaker", { userId, bytes: chunk.length });
      frameCount++;
      emit({ event: "audio", user_id: userId, pcm_b64: chunk.toString("base64") });
    });
    const cleanup = () => {
      speakerSubscriptions.delete(userId);
      diag("speaker stream ended", { userId, frameCount });
    };
    opusStream.on("end", cleanup);
    opusStream.on("close", cleanup);
    opusStream.on("error", (e) => { fail(`opus stream: ${e.message}`); cleanup(); });
    decoder.on("error", (e) => fail(`opus decode: ${e.message}`));
    downsampler.on("error", (e) => fail(`downsampler: ${e.message}`));
  });
}

function setupPlayback(conn) {
  player = createAudioPlayer();
  conn.subscribe(player);

  playbackStream = new Readable({ read() {} });
  const upsampler = new prism.FFmpeg({
    args: [
      "-analyzeduration", "0",
      "-loglevel", "0",
      "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "-",
      "-ar", "48000", "-ac", "2", "-f", "s16le",
    ],
  });
  upsampler.on("error", (e) => fail(`upsampler: ${e.message}`));
  playbackStream.pipe(upsampler);

  const resource = createAudioResource(upsampler, { inputType: StreamType.Raw });
  player.play(resource);
}

function doLeave() {
  if (connection) {
    diag("doLeave — destroying connection");
    try { connection.destroy(); } catch {}
    connection = null;
  }
  if (playbackStream) {
    try { playbackStream.push(null); } catch {}
    playbackStream = null;
  }
  player = null;
  speakerSubscriptions.clear();
  emit({ event: "left" });
}

function doPlay({ pcm_b64 }) {
  if (!playbackStream || !pcm_b64) return;
  playbackStream.push(Buffer.from(pcm_b64, "base64"));
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", async (line) => {
  let cmd;
  try { cmd = JSON.parse(line); }
  catch (e) { fail(`bad JSON: ${e.message}`); return; }

  try {
    switch (cmd.action) {
      case "join":     await doJoin(cmd); break;
      case "leave":    doLeave(); break;
      case "play":     doPlay(cmd); break;
      case "shutdown": process.exit(0);
      default:         fail(`unknown action: ${cmd.action}`);
    }
  } catch (e) {
    fail(`${cmd.action || "?"} failed: ${e.message}`);
  }
});

rl.on("close", () => process.exit(0));

try {
  await client.login(BOT_TOKEN);
} catch (e) {
  fail(`login failed: ${e.message}`);
  process.exit(1);
}
