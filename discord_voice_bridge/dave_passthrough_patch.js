/**
 * Runtime patch for @discordjs/voice 0.19.x DAVE bugs.
 *
 * Replaces DAVESession.prototype.decrypt to fix two issues that prevent the bot
 * from hearing audio in channels where DAVE is enabled. DAVE has been the
 * Discord default since March 2026 and is now mandatory; opting out with
 * daveEncryption:false is rejected with close code 4017.
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
 * Upstream PR #11449 ("strip padding from packets and add guards") targets
 * voice 0.20.0, not 0.19.x. Until that release ships, this patch matches the
 * behavior of the confirmed-working workaround on
 *   https://github.com/discordjs/discord.js/issues/11419
 * (stevenpetryk, 2026-03-11) and additionally fixes bug #2 above.
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

  const canDecrypt =
    this.session?.ready &&
    (this.protocolVersion !== 0 || this.session?.canPassthrough(userId));
  if (!canDecrypt || !this.session) return packet;

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

process.stderr.write(
  "[voice-bridge] dave_passthrough_patch applied — DAVESession.decrypt overridden\n"
);
