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
  AudioPlayerStatus,
  NoSubscriberBehavior,
  entersState,
} from "@discordjs/voice";
import prism from "prism-media";
import { Readable } from "node:stream";
import readline from "node:readline";

// Apply the DAVE passthrough patch BEFORE any voice connection is created.
// See dave_passthrough_patch.js header for context. Removable once
// @discordjs/voice >= 0.20.0 ships PR #11449.
import "./dave_passthrough_patch.js";

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

// AUDIO TELEMETRY (stage C): outbound playback counters. Silence used to be
// invisible here — doPlay returned without a word when player/connection were
// absent. Now every drop is counted and surfaced.
let playRx = 0;
let playBytes = 0;
let playDropped = 0;

// Deafness detector. After a successful join the WebSocket can report a healthy
// "connected" state while every inbound audio burst decodes to ZERO frames —
// the failure mode observed 2026-06-11 when DAVE decrypt / Opus decode broke at
// the MLS transition. Count consecutive speaking bursts that yielded no frames;
// past the threshold, surface a hard error to Python instead of failing silently.
// Reset on any decoded frame and on every fresh join/leave.
let consecutiveEmptySpeakingBursts = 0;
const EMPTY_BURST_DEAF_THRESHOLD = 3;

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
      // Distinct event (not a generic error): the parent must reset its
      // VoiceController from IN_VOICE back to DISCONNECTED so it stops routing
      // audio into a dead pipeline and re-joins on the next utterance / reconcile.
      emit({ event: "voice_lost", message: "voice connection lost and could not recover" });
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
    // Use the library default tolerance (36). The earlier value of 5 was based
    // on a misread of the library — `consecutiveFailures` resets on a
    // successful decrypt, not on AfterSilence — and it triggered the
    // lastTransitionId-falsy bug on the very first utterance. The real fix
    // for that bug is in dave_passthrough_patch.js (imported above), which
    // also enables passthrough for the in-flight MLS-transition packets
    // that Davey would otherwise reject as UnencryptedWhenPassthroughDisabled.
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
  consecutiveEmptySpeakingBursts = 0;

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
      // Real audio is flowing — the receive path is healthy; clear the deaf counter.
      consecutiveEmptySpeakingBursts = 0;
      emit({ event: "audio", user_id: userId, pcm_b64: chunk.toString("base64") });
    });
    let cleanedUp = false;
    const cleanup = () => {
      if (cleanedUp) return;
      cleanedUp = true;
      speakerSubscriptions.delete(userId);
      diag("speaker stream ended", { userId, frameCount });
      if (frameCount > 0) {
        consecutiveEmptySpeakingBursts = 0;
        return;
      }
      // The user spoke (we subscribed) but decoded nothing. One empty burst is
      // normal noise; a run of them means the receive path is broken even though
      // the connection looks healthy. Surface it loudly instead of dying silent.
      consecutiveEmptySpeakingBursts++;
      if (consecutiveEmptySpeakingBursts >= EMPTY_BURST_DEAF_THRESHOLD) {
        fail(
          `deaf: decoded 0 audio frames across ${consecutiveEmptySpeakingBursts} ` +
          `consecutive speaking bursts since join — voice receive is broken ` +
          `(likely DAVE decrypt / Opus decode failure)`
        );
        consecutiveEmptySpeakingBursts = 0; // re-arm; don't spam every burst
      }
    };
    opusStream.on("end", cleanup);
    opusStream.on("close", cleanup);
    opusStream.on("error", (e) => { fail(`opus stream: ${e.message}`); cleanup(); });
    // Route decode/resample errors through cleanup too: a single bad packet must
    // free the subscription (so the next speaking event rebuilds it) and feed the
    // deaf detector, rather than wedging a half-dead pipeline in speakerSubscriptions.
    decoder.on("error", (e) => { fail(`opus decode: ${e.message}`); cleanup(); });
    downsampler.on("error", (e) => { fail(`downsampler: ${e.message}`); cleanup(); });
  });
}

// Playback architecture:
// The discord.js AudioPlayer kills an AudioResource after maxMissedFrames
// (default 100ms) of no data. The resource then enters silence-padding mode
// and dies permanently — pushing more PCM into a stream that feeds a dead
// resource produces no sound. So we rebuild playbackStream + FFmpeg upsampler
// + AudioResource on every audio burst, and tear them down when the player
// goes Idle. The player itself stays alive for the lifetime of the voice
// connection so the "speaking" indicator behaves correctly (only on while
// Aria is actually producing audio).
function setupPlayback(conn) {
  player = createAudioPlayer({
    behaviors: {
      noSubscriber: NoSubscriberBehavior.Play,
    },
  });
  conn.subscribe(player);

  player.on(AudioPlayerStatus.Idle, () => {
    teardownPlaybackStream("player idle");
  });
  player.on("error", (e) => {
    diag("audio player error", { message: e.message });
    teardownPlaybackStream("player error");
  });
}

function teardownPlaybackStream(reason) {
  if (!playbackStream) return;
  diag("teardown playback stream", { reason });
  try { playbackStream.push(null); } catch {}
  try { playbackStream.destroy(); } catch {}
  playbackStream = null;
}

function ensurePlaybackStream() {
  if (playbackStream) return playbackStream;
  if (!player || !connection) return null;

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
    // FFmpeg upsamplers sometimes emit "Premature close" when their consumer
    // detaches between bursts. That is expected — we already tore down on the
    // player Idle event. Log at diag level so we keep the audit trail but
    // do not propagate it to Python as a hard error event.
    diag("upsampler error (non-fatal)", { message: e.message });
  });
  stream.pipe(upsampler);

  // silencePaddingFrames (default 5) gives 100ms of trailing silence after
  // the last real PCM packet. That window is also when the player decides
  // to call stop() on the resource (after `maxMissedFrames` of no data),
  // which causes a clean transition to Idle. We then rebuild on the next
  // doPlay. Setting padding to 0 is aggressive and can wedge the resource
  // before FFmpeg's first flush on a small burst.
  const resource = createAudioResource(upsampler, {
    inputType: StreamType.Raw,
  });
  player.play(resource);
  playbackStream = stream;
  diag("new playback burst started");
  return playbackStream;
}

function doLeave() {
  if (connection) {
    diag("doLeave — destroying connection");
    try { connection.destroy(); } catch {}
    connection = null;
  }
  teardownPlaybackStream("leave");
  if (player) {
    try { player.stop(true); } catch {}
  }
  player = null;
  speakerSubscriptions.clear();
  consecutiveEmptySpeakingBursts = 0;
  emit({ event: "left" });
}

// Authoritative voice presence from THIS bot's independent Discord gateway.
// The py-cord parent's cached voice states go stale after a bare gateway RESUME
// (exactly when the parent's reconcile loop must act). This sidecar is a second,
// independent gateway connection, so its voiceStates cache is unlikely to be
// stale at the same instant. Scan every guild for the authorized user and report
// which voice channel (if any) they are in.
function doQueryPresence() {
  let channelId = null;
  let channelName = null;
  if (AUTHORIZED_USER_ID) {
    for (const guild of client.guilds.cache.values()) {
      const vs = guild.voiceStates.cache.get(AUTHORIZED_USER_ID);
      if (vs && vs.channelId) {
        channelId = vs.channelId;
        channelName = vs.channel?.name ?? null;
        break;
      }
    }
  }
  emit({ event: "presence", channel_id: channelId, channel_name: channelName });
}

function doPlay({ pcm_b64 }) {
  if (!pcm_b64) return;
  const buf = Buffer.from(pcm_b64, "base64");
  if (!player || !connection) {
    playDropped++;
    if (playDropped === 1 || playDropped % 50 === 0) {
      diag("AUDIO[C doPlay]: DROPPING audio — no player/connection", {
        dropped: playDropped, bytes: buf.length, hasPlayer: !!player, hasConn: !!connection,
      });
    }
    return;
  }
  const stream = ensurePlaybackStream();
  if (!stream) {
    playDropped++;
    diag("AUDIO[C doPlay]: no playback stream", { dropped: playDropped });
    return;
  }
  playRx++;
  playBytes += buf.length;
  if (playRx === 1 || playRx % 100 === 0) {
    diag("AUDIO[C doPlay]: pushed chunk", { n: playRx, bytes: buf.length, total: playBytes });
  }
  stream.push(buf);
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", async (line) => {
  let cmd;
  try { cmd = JSON.parse(line); }
  catch (e) { fail(`bad JSON: ${e.message}`); return; }

  try {
    switch (cmd.action) {
      case "join":           await doJoin(cmd); break;
      case "leave":          doLeave(); break;
      case "play":           doPlay(cmd); break;
      case "query_presence": doQueryPresence(); break;
      case "shutdown":       process.exit(0);
      default:               fail(`unknown action: ${cmd.action}`);
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
