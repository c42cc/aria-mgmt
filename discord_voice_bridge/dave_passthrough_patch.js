/**
 * Runtime patch for @discordjs/voice 0.19.x DAVE bugs.
 *
 * Replaces DAVESession.prototype.decrypt (receive) AND .encrypt (send) to fix
 * issues that prevent the bot from hearing — and being heard — in channels
 * where DAVE is enabled. DAVE has been the Discord default since March 2026 and
 * is now mandatory; opting out with daveEncryption:false is rejected with close
 * code 4017.
 *
 * SEND side (encrypt): upstream's encrypt() is naked —
 *     encrypt(packet){ if (v0 || !session.ready || silence) return packet;
 *                       return session.encryptOpus(packet); }
 * Two outbound-silence traps: (1) when the MLS session is not `ready` it returns
 * the packet UNENCRYPTED, which Discord drops in a DAVE channel — so the bot is
 * silent with no error; (2) encryptOpus() can throw (e.g. mid MLS transition)
 * and, unlike decrypt, there is no guard, so the throw can break the outbound
 * dispatch. The caller is `daveSession?.encrypt(opusPacket) ?? opusPacket`
 * (voice dist L1738), so returning null safely falls back to plaintext and the
 * send stream survives. The patch keeps the happy path identical, adds a guard,
 * and — crucially — makes both silence traps LOUD so "voice doesn't work" can
 * never hide again.
 *
 *   1. "DecryptionFailed(UnencryptedWhenPassthroughDisabled)" — Discord's DAVE
 *      protocol permits unencrypted opus packets during MLS transitions
 *      (passthrough mode). The library throws on these packets instead of
 *      passing them through, and VoiceReceiver.onUdpMessage propagates the
 *      throw via stream.destroy(error), killing the per-speaker opus stream.
 *
 *   2. `if (this.lastTransitionId)` treats 0 as falsy. After the initial
 *      transition (transition_id=0), lastTransitionId is set to 0, so the
 *      recovery branch is skipped and the throw fires the first time the
 *      decryption-failure tolerance is exceeded.
 *
 *   3. The earlier workaround returned the RAW packet whenever the session
 *      could not decrypt yet (`if (!canDecrypt || !this.session) return packet`).
 *      When a DAVE session exists but is mid-transition, that raw packet is
 *      still ciphertext, and feeding ciphertext to the Opus decoder throws
 *      "Decode error: Invalid packet", which destroys the per-speaker opus
 *      stream and leaves Aria deaf (observed 2026-06-11 at MLS transition_id 0:
 *      "MLS commit processed" immediately followed by "opus decode: Invalid
 *      packet", then frameCount:0 on every burst). The invariant now enforced:
 *      decrypt() returns ONLY valid Opus plaintext or null — never ciphertext.
 *      A raw passthrough is allowed only when there is no DAVE session (plain
 *      Opus) or the session itself reports an unencrypted passthrough packet.
 *      Otherwise we DROP (return null): the stream survives and audio resumes
 *      once the session is ready.
 *
 * Upstream PR #11449 ("strip padding from packets and add guards") targets
 * voice 0.20.0, not 0.19.x. No 0.20.0 stable has shipped on npm (latest is
 * 0.19.2; newer builds are 1.0.0-dev prereleases of the discord.js v15 line).
 * Until a stable release ships, this patch matches the behavior of the
 * confirmed-working workaround on
 *   https://github.com/discordjs/discord.js/issues/11419
 * (stevenpetryk, 2026-03-11) and additionally fixes bugs #2 and #3 above.
 *
 * Remove this file once @discordjs/voice >= 0.20.0 ships PR #11449.
 */

import Davey from "@snazzah/davey";
import { DAVESession } from "@discordjs/voice";

if (!DAVESession || typeof DAVESession.prototype.decrypt !== "function") {
  throw new Error(
    "dave_passthrough_patch: DAVESession.prototype.decrypt is missing — " +
    "the @discordjs/voice export surface changed. Remove this patch or update it."
  );
}

// Well-known opus silence frame Discord sends to keep VoIP streams alive.
// Hardcoded because the library does not export it; it has been stable for years.
const SILENCE_FRAME = Buffer.from([0xf8, 0xff, 0xfe]);

DAVESession.prototype.decrypt = function patchedDecrypt(packet, userId) {
  if (packet.length === SILENCE_FRAME.length && packet.equals(SILENCE_FRAME)) {
    return packet;
  }

  // No DAVE session at all → DAVE is not active for this stream, so the packet
  // is plain Opus. Passing it through is correct.
  if (!this.session) return packet;

  // A DAVE session exists but we are not yet able to decrypt (session not
  // ready, or a protocol-0 user without passthrough). The packet is almost
  // certainly ciphertext. Returning it raw here is what fed the Opus decoder
  // garbage and produced "Invalid packet" at the MLS transition boundary.
  // DROP it instead — never hand non-Opus bytes downstream. The per-speaker
  // stream survives and real audio resumes once the session is ready.
  const canDecrypt =
    this.session.ready &&
    (this.protocolVersion !== 0 || this.session.canPassthrough(userId));
  if (!canDecrypt) return null;

  try {
    const buffer = this.session.decrypt(userId, Davey.MediaType.AUDIO, packet);
    this.consecutiveFailures = 0;
    return buffer;
  } catch (error) {
    // The packet is unencrypted opus from an MLS transition window. Discord's
    // spec explicitly permits this; the library bug is that passthrough is off
    // by default for fresh sessions. Pass it through unchanged.
    if (error?.message?.includes?.("UnencryptedWhenPassthroughDisabled")) {
      this.consecutiveFailures = 0;
      return packet;
    }

    if (!this.reinitializing && this.pendingTransitions.size === 0) {
      this.consecutiveFailures++;
      this.emit(
        "debug",
        `[patched] Failed to decrypt a packet (${this.consecutiveFailures} consecutive fails)`
      );
      if (this.consecutiveFailures > this.failureTolerance) {
        // Bug fix: treat lastTransitionId === 0 as a valid recovery target.
        // Upstream uses a truthy check which silently skips recovery after
        // the initial transition (id=0) and falls through to `throw error`.
        if (this.lastTransitionId !== undefined) {
          this.recoverFromInvalidTransition(this.lastTransitionId);
        }
        // Never throw. Dropping a packet beats destroying the opus stream;
        // the stream survives, Discord re-keys via MLS, real audio resumes.
      }
    } else if (this.reinitializing) {
      this.emit("debug", "[patched] Failed to decrypt a packet (reinitializing session)");
    } else {
      this.emit(
        "debug",
        `[patched] Failed to decrypt a packet (${this.pendingTransitions.size} pending transition[s])`
      );
    }

    return null;
  }
};

// --------------------------------------------------------------------------
// SEND side: guarded, loud encrypt. Mirrors the decrypt philosophy — never
// throw, drop at worst, and surface the two outbound-silence traps.
// --------------------------------------------------------------------------
if (typeof DAVESession.prototype.encrypt !== "function") {
  throw new Error(
    "dave_passthrough_patch: DAVESession.prototype.encrypt is missing — " +
    "the @discordjs/voice export surface changed. Remove this patch or update it."
  );
}

let _encOk = 0;
let _encNotReady = 0;
let _encErr = 0;

DAVESession.prototype.encrypt = function patchedEncrypt(packet) {
  // Upstream fast-paths, unchanged.
  if (this.protocolVersion === 0) return packet;
  if (packet.length === SILENCE_FRAME.length && packet.equals(SILENCE_FRAME)) {
    return packet;
  }

  if (!this.session?.ready) {
    // Outbound goes UNENCRYPTED here and Discord drops it in a DAVE channel.
    // This is the #1 "voice is silent but no error" trap. Be loud.
    _encNotReady++;
    if (_encNotReady === 1 || _encNotReady % 100 === 0) {
      process.stderr.write(
        "[voice-bridge] AUDIO[encrypt]: DAVE session NOT ready — " +
        `${_encNotReady} outbound frame(s) unencrypted; Discord drops these, ` +
        "so Aria is inaudible until the MLS group reaches ready.\n"
      );
      try { this.emit("debug", "[patched-encrypt] session not ready — unencrypted passthrough"); } catch {}
    }
    return packet;
  }

  try {
    const out = this.session.encryptOpus(packet);
    _encOk++;
    if (_encOk === 1) {
      process.stderr.write("[voice-bridge] AUDIO[encrypt]: first frame ENCRYPTED ok — outbound E2EE live\n");
    } else if (_encOk % 500 === 0) {
      process.stderr.write(`[voice-bridge] AUDIO[encrypt]: encrypted x${_encOk}\n`);
    }
    return out;
  } catch (error) {
    // Never let an encrypt throw break the outbound dispatch. Returning null
    // makes the caller (`?? opusPacket`) fall back to plaintext; the stream
    // survives and Discord re-keys via MLS.
    _encErr++;
    if (_encErr === 1 || _encErr % 50 === 0) {
      process.stderr.write(
        `[voice-bridge] AUDIO[encrypt]: encryptOpus threw x${_encErr}: ${error?.message}\n`
      );
      try { this.emit("debug", `[patched-encrypt] encryptOpus threw: ${error?.message}`); } catch {}
    }
    return null;
  }
};

process.stderr.write(
  "[voice-bridge] dave_passthrough_patch applied — DAVESession.decrypt + .encrypt overridden\n"
);
