"""Main entry point. The whole loop."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Coroutine

import discord
from discord.ext import commands

from .config import config
from .conversation import conversation
from .cursor_bridge import CursorBridge
from .cursor_external import CursorExternalObserver
from .cursor_registry import CursorAgent, RegistryEvent, cursor_registry
from .db import (
    init_db, get_daily_spend, log_event, upsert_cursor_session,
    update_cursor_session_event, bind_thread, session_for_thread,
)
from .discord_voice import VoiceTransitionBusy, voice_bridge, voice_controller
from .gemini_session import GeminiSession
from .local_audio import SpeakerOutput
from .memory import init_memory, remember, recall

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
cursor_bridge = CursorBridge()
gemini: GeminiSession | None = None

_cancel_flag = False
_audio_tasks: list[asyncio.Task] = []
_last_audio_received_at: float = 0.0
_session_paused: bool = False
_pause_transcript: str = ""

_preflight_passed: bool = False
_last_preflight_report: object | None = None

_spicylit_active: bool = False
_grok_session = None
_grok_session_started_at: float = 0.0
_grok_paused: bool = False

_pending_voice_channel_id: str | None = None

# Background task that follows the authorized user into voice after a bare
# Discord gateway RESUME (which re-establishes the session WITHOUT re-firing
# on_ready, so the post-preflight rescan that normally follows the user is
# skipped). Started once in on_ready's boot-only init.
_voice_reconcile_task: asyncio.Task | None = None
_VOICE_RECONCILE_INTERVAL_SEC = 30.0

# Background task that durably judges session records the inline fire-and-forget
# task used to drop on process churn. Started once in on_ready's boot-only init.
_judge_sweep_task: asyncio.Task | None = None
_JUDGE_SWEEP_INTERVAL_SEC = 120.0

_wake_listener = None          # WakeWordListener | None (typed loosely to avoid import at module level)
_local_speaker: SpeakerOutput | None = None
_local_session_active: bool = False
_local_silence_task: asyncio.Task | None = None

_on_ready_done = False

cursor_observer: CursorExternalObserver | None = None

IDLE_TIMEOUT_SEC = 25
GROK_IDLE_TIMEOUT_SEC = 25
GROK_COST_PER_MINUTE = 0.05
VOICE_EXIT_TIMEOUT_SEC = 600  # leave voice after 10 min of total silence
LOCAL_SILENCE_TIMEOUT_SEC = 8  # close local voice session after 8s idle

CONFIRM_TIMEOUT_SEC = 60  # tier-X/I confirmation wait window
CONFIRM_APPROVE_EMOJIS = {"\u2705", "\U0001f44d"}  # white check mark, thumbs up
CONFIRM_DECLINE_EMOJIS = {"\u274c", "\U0001f44e"}  # cross mark, thumbs down


# ---------------------------------------------------------------------------
# Tier-X/I confirmation: text + reaction approval registry
# ---------------------------------------------------------------------------
# The Gemini confirm_action function call (gemini_session.py) is one
# approval channel. A task initiated from #ucs text with no live voice
# session cannot be approved through Gemini — there is no receive loop to
# carry the confirm_action back. This registry adds a second answer
# channel: !ok/!no in #ucs-alerts, or a reaction on the confirmation card.
# bot._confirm_callback races Gemini wait_for_confirmation against
# discord_wait_for_confirmation; whichever resolves first wins.

@dataclass
class _PendingConfirmation:
    """A tier-X/I action awaiting approval via Discord text/reaction.

    The `event` is set by `_resolve_pending_confirmation` when the user
    answers via !ok/!no or a thumbs-up/check / thumbs-down/cross reaction.
    `message_id` is the alerts-card message we watch reactions on.
    """
    action_id: str
    tool_name: str
    summary: str
    event: asyncio.Event
    message_id: int | None = None
    result: dict = field(default_factory=dict)


_pending_text_confirmations: dict[str, _PendingConfirmation] = {}
# Reverse map message_id -> action_id so on_reaction_add can find the
# pending confirmation by the reacted message without scanning the dict.
_confirmation_message_index: dict[int, str] = {}

# True while any do_with_claude loop or text-confirmation is in flight.
# The Gemini idle-pause watchdog (_watchdog_task) and the Gemini session
# close path (_do_close) consult this so they don't tear down work the
# user is in the middle of approving or watching run.
_loops_in_flight: int = 0


def _register_pending_confirmation(
    action_id: str, tool_name: str, summary: str
) -> _PendingConfirmation:
    pending = _PendingConfirmation(
        action_id=action_id,
        tool_name=tool_name,
        summary=summary,
        event=asyncio.Event(),
    )
    _pending_text_confirmations[action_id] = pending
    return pending


def _unregister_pending_confirmation(action_id: str) -> None:
    pending = _pending_text_confirmations.pop(action_id, None)
    if pending and pending.message_id is not None:
        _confirmation_message_index.pop(pending.message_id, None)


def _resolve_pending_confirmation(
    action_id: str, approved: bool, source: str
) -> bool:
    """Set the pending confirmation result. Returns False if no such id."""
    pending = _pending_text_confirmations.get(action_id)
    if not pending:
        return False
    if pending.event.is_set():
        return True
    pending.result = {"approved": approved, "source": source}
    pending.event.set()
    return True


async def _discord_wait_for_confirmation(
    action_id: str, timeout: float
) -> dict:
    """Block until !ok/!no or a reaction resolves action_id, or timeout."""
    pending = _pending_text_confirmations.get(action_id)
    if not pending:
        return {"approved": False, "timeout": True}
    try:
        await asyncio.wait_for(pending.event.wait(), timeout=timeout)
        return pending.result or {"approved": False}
    except asyncio.TimeoutError:
        return {"approved": False, "timeout": True}


async def _race_confirmations(
    voice_waiter: Coroutine, text_waiter: Coroutine
) -> dict:
    """Run voice and text waiters concurrently; first decisive answer wins.

    "Decisive" means a real approval/decline. If one waiter times out and
    the other is still pending, we keep waiting on the other. If both
    time out, the result is `{"approved": False, "timeout": True}`.
    """
    voice_task = asyncio.create_task(voice_waiter, name="confirm_voice")
    text_task = asyncio.create_task(text_waiter, name="confirm_text")
    pending = {voice_task, text_task}
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    res = task.result()
                except Exception:
                    log.exception("confirmation waiter raised")
                    continue
                if not res.get("timeout"):
                    return res
        return {"approved": False, "timeout": True}
    finally:
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Local voice session (wake word → Gemini via Mac mic/speakers)
# ---------------------------------------------------------------------------

async def _on_wake_word() -> None:
    """Called by WakeWordListener when the wake word is detected."""
    global _local_session_active, _local_speaker, _local_silence_task

    if _local_session_active:
        return
    if voice_controller.in_voice:
        log.debug("Wake word heard but Discord voice is active — ignoring")
        return
    if not gemini:
        log.warning("Wake word heard but Gemini not constructed yet")
        return

    log.info("Wake word detected — opening local Gemini session")
    _local_session_active = True

    if _wake_listener:
        _wake_listener.pause()

    try:
        if not gemini.connected:
            await gemini.connect()
    except Exception:
        log.exception("Failed to connect Gemini on wake word")
        _local_session_active = False
        if _wake_listener:
            _wake_listener.resume()
        return

    if _wake_listener:
        _wake_listener.set_forward_callback(gemini.send_audio)

    _local_speaker = SpeakerOutput()
    _local_speaker.start(gemini)

    _local_silence_task = asyncio.create_task(
        _local_silence_watchdog(), name="local_silence_wd"
    )
    log.info("Local voice session active")


async def _local_silence_watchdog() -> None:
    """Close local session after LOCAL_SILENCE_TIMEOUT_SEC of no Gemini output."""
    try:
        await asyncio.sleep(3)
        while _local_session_active:
            await asyncio.sleep(2)
            if _local_speaker and _local_speaker.last_output_at > 0:
                idle = time.monotonic() - _local_speaker.last_output_at
                if idle > LOCAL_SILENCE_TIMEOUT_SEC:
                    break
    except asyncio.CancelledError:
        return

    log.info("%ds silence — closing local voice session", LOCAL_SILENCE_TIMEOUT_SEC)
    await _close_local_session()


async def _close_local_session() -> None:
    global _local_session_active, _local_speaker, _local_silence_task

    _local_session_active = False

    if _wake_listener:
        _wake_listener.set_forward_callback(None)

    if _local_speaker:
        await _local_speaker.stop()
        _local_speaker = None

    if gemini and gemini.connected:
        try:
            await gemini.close()
        except Exception:
            log.exception("Error closing Gemini after local session")

    if _local_silence_task and not _local_silence_task.done():
        _local_silence_task.cancel()
    _local_silence_task = None

    if _wake_listener:
        _wake_listener.resume()

    log.info("Local voice session closed — wake word listening resumed")


# ---------------------------------------------------------------------------
# Discord text helpers
# ---------------------------------------------------------------------------

_CONTROL_PLANE_FRIENDLY = (
    "I hit an internal error and didn't get to your task. "
    "Try `!retry` or `!stop` to clear state."
)


def _looks_like_control_plane_error(result: str) -> bool:
    """P4: True if `result` is a host/loop error string that should NOT reach the user verbatim.

    Recognises the three shapes seen in production:
    - re-entrancy lock: `{"error": "An agent loop is already running ..."}`
    - raw Anthropic SDK errors: `Error code: 400 - {...}`
    - any single-line JSON whose top-level object has exactly one key, `error`

    Tool-side typed errors (`_error_class`) are NOT control-plane errors —
    those are Aria's job to handle in the loop. This matcher only catches
    errors that escaped the loop without becoming a real reply.
    """
    if not result:
        return True
    stripped = result.strip()
    if not stripped:
        return True
    if stripped.startswith("Error code:"):
        return True
    if stripped.startswith("{") and "\n" not in stripped:
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return False
        if isinstance(obj, dict):
            # Sole-key `error` envelope is the canonical control-plane shape.
            if list(obj.keys()) == ["error"]:
                return True
            # _error_class envelopes are tool-typed errors — Aria should
            # have handled these. They are NOT control-plane.
            if "_error_class" in obj:
                return False
            # Some legacy paths returned `{"error": "...", "trace": "..."}`.
            # If `error` is the dominant field, still quarantine.
            if "error" in obj and len(obj) <= 2:
                return True
    return False


def _clip(text: str, max_len: int) -> str:
    """Truncate `text` to <= max_len chars at a clean boundary, with an ellipsis.

    Decision/proposal cards quote a thread's last message so Corbin has the
    context to approve or skip. The old `text[:n]` slices cut those quotes
    mid-word — often mid-sentence, right before the detail that justified the
    decision — and dropped the rest silently. Because the slice happened
    *before* the card was sent, the discarded text never reached Discord at
    all: the card looked complete but the substance was gone.

    This prefers a sentence end, then a word boundary, in the back half of the
    window (so a boundary-free run can't collapse the quote to almost nothing),
    and appends a single ellipsis so the cut is visibly a cut rather than a
    surprise stop.
    """
    body = text.strip()
    if len(body) <= max_len:
        return body
    window = body[: max_len - 1]  # reserve one char for the ellipsis
    floor = max_len // 2
    sentence_cut = max(window.rfind(". "), window.rfind("! "),
                       window.rfind("? "), window.rfind("\n"))
    if sentence_cut >= floor:
        return window[: sentence_cut + 1].rstrip() + "\u2026"
    space_cut = window.rfind(" ")
    if space_cut >= floor:
        return window[:space_cut].rstrip() + "\u2026"
    return window.rstrip() + "\u2026"


def _split_at_paragraphs(text: str, max_len: int = 1900) -> list[str]:
    """Split text into Discord-safe chunks (<= max_len), preferring paragraph
    then line boundaries. Falls back to a hard slice for runs that contain
    no boundary at all, so the result is guaranteed never to exceed max_len —
    Discord's hard limit is 2000 and the default 1900 leaves headroom.

    Without the line/hard-slice fallback, a single long paragraph (e.g. the
    preflight report banner) would be returned intact and Discord would
    reject the send with HTTPException 50035.
    """
    if len(text) <= max_len:
        return [text]

    def _hard_slice(s: str) -> list[str]:
        return [s[i : i + max_len] for i in range(0, len(s), max_len)]

    def _split_line(line: str) -> list[str]:
        return _hard_slice(line) if len(line) > max_len else [line]

    def _flush(current: str, chunks: list[str]) -> str:
        if current:
            chunks.append(current.strip())
        return ""

    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        if len(para) <= max_len:
            if len(current) + len(para) + 2 > max_len:
                current = _flush(current, chunks)
            current = current + "\n\n" + para if current else para
            continue
        # Paragraph itself is too long: flush, then split by lines.
        current = _flush(current, chunks)
        for line in para.split("\n"):
            for piece in _split_line(line):
                if len(current) + len(piece) + 1 > max_len:
                    current = _flush(current, chunks)
                current = current + "\n" + piece if current else piece
    if current.strip():
        chunks.append(current.strip())
    return chunks or [text[:max_len]]


async def post_to_text(content: str, thread: discord.Thread | None = None) -> None:
    """Post content to #ucs (or a thread). Handles chunking and file fallback.

    This is a low-level Discord posting function used by many callers
    (build threads, tool results, plan outputs, etc.). It does NOT record
    into the conversation buffer — callers that represent actual
    conversational replies (like _handle_text_conversation and _run_ask)
    are responsible for recording their own turns. This prevents build
    thread noise and tool dumps from polluting the buffer.
    """
    ch = bot.get_channel(int(config.discord_text_channel_id))
    if not ch:
        raise RuntimeError(f"Text channel {config.discord_text_channel_id} not found — bot cannot post")
    await _send_chunked(thread or ch, content)


async def _send_chunked(target, content: str) -> None:
    """Send possibly-long content to any messageable target — a request
    thread, a channel, or a DM — chunked at paragraph boundaries, with a
    file attachment for very long output. One sender for every conversational
    reply so a thread, a #ucs post, and a DM all chunk identically.
    """
    if not content:
        return
    if len(content) > 6000:
        await target.send(
            "Full result attached:",
            file=discord.File(io.BytesIO(content.encode()), filename="result.md"),
        )
    else:
        for chunk in _split_at_paragraphs(content):
            await target.send(chunk)


async def post_to_alerts(content: str, *, silent: bool = False) -> None:
    """Post to #ucs-alerts. Recorded in the conversation buffer as a
    system alert so text-Aria can answer 'what was that error?' from
    `#ucs`.

    silent=True suppresses the push/badge/sound notification on the post.
    Use it when an audit-trail record duplicates a louder primary
    notification (e.g. the cursor pager that also fires a DM or a voice
    narration on the same event).
    """
    ch = bot.get_channel(int(config.discord_log_channel_id))
    if not ch:
        raise RuntimeError(f"Alert channel {config.discord_log_channel_id} not found — bot cannot post alerts")
    # #ucs-alerts is a no-notification STREAM (Corbin's rule): glance at it any
    # time, but it must never buzz. Decisions that warrant a buzz go to the main
    # channel via propose_action. We force silent here regardless of caller.
    for chunk in _split_at_paragraphs(content):
        await ch.send(chunk, silent=True)
    conversation.add_alert(content)


async def _emit_progress_to_user(text: str, session_key: str = "") -> None:
    """Progress-spine sink injected into tools.init_tools.

    A step lands in the request's OWN thread (session_key == the Discord
    thread id), so the live trail sits right under the request the user is
    watching — which makes the ack "I'll show each step here" literally
    true instead of secretly routing the steps to a different channel.

    Only when there is no thread to post into — a voice/global loop with an
    empty session_key, or a thread the gateway cache hasn't surfaced yet —
    does the step go to the #ucs-alerts catch-all. A connected Gemini voice
    session also gets the step as background context so Aria narrates
    "still working" coherently and the session stays warm.

    Strictly non-fatal. Posted directly (not via post_to_alerts) so it is
    never recorded in the conversation buffer — that is reserved for real
    turns, not tool-by-tool narration that would drown out Claude/Gemini
    context on the next request.
    """
    posted = False
    try:
        if session_key and str(session_key).isdigit():
            target = bot.get_channel(int(session_key))
            if target is not None:
                await target.send(text, silent=True)
                posted = True
    except Exception:
        log.debug("progress thread post failed (non-fatal)", exc_info=True)
    if not posted:
        try:
            alerts = bot.get_channel(int(config.discord_log_channel_id))
            if alerts is not None:
                await alerts.send(text, silent=True)
        except Exception:
            log.debug("progress alerts post failed (non-fatal)", exc_info=True)
    try:
        if gemini and gemini.connected:
            await gemini.inject_text(f"[progress] {text}", turn_complete=False)
    except Exception:
        log.debug("progress voice inject failed (non-fatal)", exc_info=True)


async def _post_confirmation_card(
    action_id: str, tool_name: str, summary: str
) -> discord.Message | None:
    """Post a tier-X/I confirmation card to #ucs-alerts and prime reactions.

    Returns the posted Message so the caller can register its id for
    reaction-based resolution. Failures are logged but do not block the
    confirmation flow — voice approval still works.
    """
    body = (
        f"**Confirmation required** (action_id=`{action_id}`)\n"
        f"`{tool_name}`: {summary}\n"
        f"Reply `!ok {action_id}` / `!no {action_id}` or react "
        f"\u2705 / \u274c. Times out in {CONFIRM_TIMEOUT_SEC}s."
    )
    ch = bot.get_channel(int(config.discord_log_channel_id))
    if not ch:
        log.error(
            "Alert channel %s not found — cannot post confirmation card",
            config.discord_log_channel_id,
        )
        return None
    message: discord.Message | None = None
    try:
        message = await ch.send(body)
    except Exception:
        log.exception("Failed to post confirmation card to #ucs-alerts")
        return None
    conversation.add_alert(body)
    try:
        await message.add_reaction("\u2705")
        await message.add_reaction("\u274c")
    except Exception:
        log.debug("Could not prime confirmation reactions (non-fatal)", exc_info=True)
    return message


async def _post_proposal_card(
    action_id: str, title: str, why: str, task: str
) -> discord.Message | None:
    """Post a tap-to-approve 'recommended approach' card to #ucs-alerts.

    Distinct wording from the tier-X/I confirmation card: this is a positive
    recommendation Corbin opts INTO, not a gate on something already decided.
    Primes the same check/cross reactions the reaction handler resolves.
    """
    # Decisions are the ONE thing allowed to buzz. They go to the MAIN channel
    # (#ucs), @mention Corbin, and post NON-silent — distinct from the silent
    # #ucs-alerts stream. DMs are off, so the main channel is the buzz surface.
    mention_id = config.authorized_user_ids[0] if config.authorized_user_ids else ""
    mention = f"<@{mention_id}> " if mention_id else ""
    # Plain language, no markdown noise — this is what shows in a phone push,
    # so it has to read like a normal sentence, not asterisk soup.
    body = (
        f"{mention}Quick decision for you: {title}\n"
        + (f"{why}\n" if why else "")
        + f"\nWhat I'd do: {_clip(task, 400)}\n\n"
        f"Tap \u2705 to go ahead, or \u274c to skip."
    )
    ch = bot.get_channel(int(config.discord_text_channel_id))
    if not ch:
        log.error("Text channel not found — cannot post decision card")
        return None
    try:
        message = await ch.send(body)
    except Exception:
        log.exception("Failed to post proposal card")
        return None
    conversation.add_alert(body)
    try:
        await message.add_reaction("\u2705")
        await message.add_reaction("\u274c")
    except Exception:
        log.debug("Could not prime proposal reactions (non-fatal)", exc_info=True)
    return message


async def _await_and_run_proposal(
    action_id: str, title: str, task: str, session_key: str
) -> None:
    """Wait for Corbin to approve a proposal, then run it autonomously."""
    try:
        res = await _discord_wait_for_confirmation(
            action_id, timeout=config.proposal_timeout_sec
        )
    finally:
        _unregister_pending_confirmation(action_id)

    if not res.get("approved"):
        # The "got it / skipping" receipt already showed where Corbin tapped.
        if res.get("timeout"):
            await post_to_alerts(
                f"The decision \u201c{title}\u201d expired with no answer.", silent=True
            )
        return

    from .tools import handle_tool_call
    try:
        result = await handle_tool_call(
            "do_with_claude",
            {"task": task, "session_key": session_key or f"proposal:{action_id}"},
        )
    except Exception:
        log.exception("Proposal execution failed for %s", action_id)
        await post_to_text(
            f"That one hit a snag while running (\u201c{title}\u201d). Details are in the log."
        )
        return
    try:
        await post_to_text(f"Done with \u201c{title}\u201d.\n\n{result}")
    except Exception:
        log.exception("Failed to post proposal result")


async def _propose_action(
    title: str, why: str = "", task: str = "", session_key: str = ""
) -> dict:
    """Push Corbin a one-tap recommendation; on approval run `task` autonomously.

    Non-blocking: posts the card + a phone DM, spawns the wait/run task, and
    returns an ack immediately so the caller's loop is never held open.
    """
    import uuid
    action_id = str(uuid.uuid4())[:8]
    pending = _register_pending_confirmation(action_id, "propose_action", title)

    card = await _post_proposal_card(action_id, title, why, task)
    if card is not None:
        pending.message_id = card.id
        _confirmation_message_index[card.id] = action_id

    # The @mention card in the main channel is the buzz; no DM (Corbin's are off).
    asyncio.create_task(
        _await_and_run_proposal(action_id, title, task, session_key),
        name=f"proposal_{action_id}",
    )
    return {
        "ok": True,
        "proposed": title,
        "action_id": action_id,
        "note": "Pushed to your phone; I'll run it the moment you approve.",
    }


async def _mirror_to_voice_chat(prefix: str, text: str) -> None:
    """Post text to the currently-active voice channel's text-in-voice chat.

    No-op when the bot is not in a voice channel. Used to mirror Aria's
    spoken replies (and the user's transcribed utterances) so the voice
    session has a scrollable transcript visible in the same Discord
    channel as the audio.

    silent=True suppresses the push/badge/sound notification for each
    mirror post. Every voice turn produces TWO calls — one for the user
    role ("You said: ...") and one for the aria role ("Aria: ..."). If
    these dinged, the user would hear two notification sounds per
    exchange ("ding ding") on top of the audio they're already hearing
    in voice. The transcript is for reference, not for alerting.
    """
    if not voice_controller.channel_id:
        return
    ch = bot.get_channel(int(voice_controller.channel_id))
    if not ch or not isinstance(ch, discord.VoiceChannel):
        return
    body = f"**{prefix}** {text}" if prefix else text
    for chunk in _split_at_paragraphs(body):
        try:
            await ch.send(chunk, silent=True)
        except discord.HTTPException:
            log.exception("Failed to mirror transcript to voice chat %s", ch.name)
            return


async def _on_voice_transcript(role: str, text: str) -> None:
    """Callback from GeminiSession on every completed voice turn.

    Records the turn in the shared conversation buffer (so text-Aria
    keeps continuity) and mirrors it to the voice channel's text chat
    (so the user has a scrollable transcript alongside the audio).
    """
    if not text:
        return

    channel_name = "voice"
    if voice_controller.channel_id:
        ch = bot.get_channel(int(voice_controller.channel_id))
        if ch is not None:
            channel_name = f"#{getattr(ch, 'name', voice_controller.channel_id)}"

    if role == "user":
        conversation.add_user_voice(channel=channel_name, text=text)
        await _mirror_to_voice_chat("You said:", text)
    elif role == "aria":
        conversation.add_aria_voice(channel=channel_name, text=text)
        await _mirror_to_voice_chat("Aria:", text)
    else:
        log.warning("Unknown transcript role %r — dropping", role)


def _resolve_discord_channel(query: str):
    """Best-effort resolver: query -> Discord TextChannel / VoiceChannel / Thread.

    Accepts:
    - A bare numeric id ("1234567890").
    - A `<#1234567890>` channel mention.
    - A channel name with optional `#` prefix.
    - Well-known aliases ("ucs", "ucs-alerts", "alerts", "spicy-lit",
      "spicylit", "voice").
    - An active thread name (substring match across all guild threads).

    Returns None when nothing matches.
    """
    if not query:
        return None
    q = query.strip()
    if q.startswith("<#") and q.endswith(">"):
        q = q[2:-1]
    if q.startswith("#"):
        q = q[1:]

    try:
        cid = int(q)
    except ValueError:
        cid = None
    if cid is not None:
        ch = bot.get_channel(cid)
        if ch:
            return ch

    aliases = {
        "ucs": config.discord_text_channel_id,
        "ucs-text": config.discord_text_channel_id,
        "text": config.discord_text_channel_id,
        "ucs-alerts": config.discord_log_channel_id,
        "alerts": config.discord_log_channel_id,
        "spicy-lit": config.discord_spicylit_channel_id,
        "spicylit": config.discord_spicylit_channel_id,
    }
    alias_id = aliases.get(q.lower())
    if alias_id:
        ch = bot.get_channel(int(alias_id))
        if ch:
            return ch

    q_lower = q.lower()
    for guild in bot.guilds:
        for ch in guild.channels:
            name = getattr(ch, "name", "")
            if name and name.lower() == q_lower:
                return ch
        for thread in guild.threads:
            tname = (thread.name or "").lower()
            if tname == q_lower or q_lower in tname:
                return thread
    return None


def _serialize_discord_message(m: discord.Message) -> dict:
    """JSON-safe projection of a Discord message. Keeps content bounded."""
    body = (m.content or "")[:2000]
    return {
        "id": str(m.id),
        "author": str(m.author),
        "author_id": str(m.author.id),
        "is_bot": bool(getattr(m.author, "bot", False)),
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "edited_at": m.edited_at.isoformat() if m.edited_at else None,
        "content": body,
        "attachments": [a.filename for a in m.attachments],
        "channel": getattr(m.channel, "name", str(getattr(m.channel, "id", ""))),
        "channel_id": str(getattr(m.channel, "id", "")),
    }


async def _fetch_discord_history(channel_query: str, limit: int) -> list[dict]:
    """Return up to `limit` most recent messages in `channel_query`, oldest-first.

    Resolves channels and threads by id, alias, name. Used by Aria's
    `discord_recent_messages` tool. Returns an empty list when the
    channel can't be resolved or the bot lacks permission to read its
    history — the tool layer wraps this in a JSON error envelope.
    """
    ch = _resolve_discord_channel(channel_query)
    if ch is None:
        raise ValueError(f"channel not found: {channel_query!r}")
    if not hasattr(ch, "history"):
        raise ValueError(
            f"channel {getattr(ch, 'name', channel_query)!r} does not support history"
        )
    msgs: list[dict] = []
    async for m in ch.history(limit=max(1, min(int(limit), 100))):
        msgs.append(_serialize_discord_message(m))
    msgs.reverse()
    return msgs


async def _fetch_discord_threads(channel_query: str) -> list[dict]:
    """List threads in a parent channel. Returns active threads only.

    Aria uses this to discover build threads (created by
    `create_build_thread` for each Cursor SDK session) and any other
    threads under `#ucs` or `#ucs-alerts`.
    """
    ch = _resolve_discord_channel(channel_query)
    if ch is None:
        raise ValueError(f"channel not found: {channel_query!r}")
    out: list[dict] = []
    raw_threads = list(getattr(ch, "threads", []))
    for guild in bot.guilds:
        for thread in guild.threads:
            parent = getattr(thread, "parent_id", None)
            if parent is not None and parent == getattr(ch, "id", None):
                if thread not in raw_threads:
                    raw_threads.append(thread)
    for thread in raw_threads:
        out.append(
            {
                "id": str(thread.id),
                "name": thread.name,
                "parent_id": str(getattr(thread, "parent_id", "")),
                "parent_name": getattr(getattr(thread, "parent", None), "name", ""),
                "created_at": thread.created_at.isoformat() if thread.created_at else None,
                "message_count": getattr(thread, "message_count", None),
                "archived": bool(getattr(thread, "archived", False)),
            }
        )
    out.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return out


async def create_build_thread(session_id: str, project: str) -> discord.Thread | None:
    """Create a Discord thread for a Cursor build session."""
    ch = bot.get_channel(int(config.discord_text_channel_id))
    if not ch or not isinstance(ch, discord.TextChannel):
        return None
    thread = await ch.create_thread(
        name=f"Build: {project} ({session_id[:8]})",
        type=discord.ChannelType.public_thread,
    )
    return thread


# ---------------------------------------------------------------------------
# Tool handler callback injection
# ---------------------------------------------------------------------------

_VOICE_VISIBLE_TOOLS = frozenset({"do_with_claude", "plan_with_claude"})

async def _handle_tool_call(name: str, args: dict) -> str:
    """Dispatch a Gemini Live tool call and mirror long results to #ucs.

    Gemini speaks the result; for long outputs the user also wants the
    verbatim text in #ucs.  That side effect lives here, at the voice-
    entry boundary, so text callers (_handle_text_conversation, _run_ask)
    that already send the result to message.channel do not trigger a
    duplicate post.
    """
    from .tools import handle_tool_call
    result = await handle_tool_call(name, args)
    if name in _VOICE_VISIBLE_TOOLS and result:
        asyncio.create_task(post_to_text(result))
    return result


# ---------------------------------------------------------------------------
# Audio pipeline tasks
# ---------------------------------------------------------------------------

async def _on_voice_audio(pcm: bytes) -> None:
    """Receive callback: 16kHz mono PCM from Discord -> Gemini or Grok.

    If a session was paused due to idle timeout, reconnect it transparently
    and resume with the saved transcript context.
    """
    global _last_audio_received_at
    global _session_paused, _pause_transcript, _grok_paused
    _last_audio_received_at = time.monotonic()
    voice_controller.touch()

    if _spicylit_active:
        if _grok_paused and _grok_session:
            log.info("User spoke — resuming paused Grok session")
            _grok_paused = False
            try:
                await _grok_session.start()
            except Exception:
                log.exception("Failed to resume Grok session")
                return
            _cancel_audio_tasks()
            _audio_tasks.append(asyncio.create_task(_drain_grok_to_voice()))
            _audio_tasks.append(asyncio.create_task(_grok_watchdog_task()))
        if _grok_session and _grok_session.connected:
            await _grok_session.send_audio(pcm)
    else:
        if _session_paused and gemini:
            log.info("User spoke — resuming paused Gemini session")
            _session_paused = False
            try:
                await gemini.connect()
            except Exception:
                log.exception("Failed to resume Gemini session")
                return
            _cancel_audio_tasks()
            _audio_tasks.append(asyncio.create_task(_drain_gemini_to_voice(gemini)))
            _audio_tasks.append(asyncio.create_task(_watchdog_task(gemini)))
            if _pause_transcript:
                try:
                    await gemini.inject_text(
                        f"Session resumed. Pick up where we left off. Recent context:\n{_pause_transcript}",
                        turn_complete=False,
                    )
                except Exception:
                    log.exception("Failed to inject resume transcript into Gemini")
                _pause_transcript = ""

            try:
                buffer_ctx = conversation.as_gemini_injection(max_turns=10)
                if buffer_ctx:
                    await gemini.inject_text(buffer_ctx, turn_complete=False)
            except Exception:
                log.exception("Failed to inject conversation buffer on resume")

            await _inject_registry_briefing(on_resume=True)
        if gemini and gemini.connected:
            await gemini.send_audio(pcm)


async def _drain_gemini_to_voice(session: GeminiSession) -> None:
    """Drain Gemini's 24kHz mono audio output and stream it to the voice sidecar.

    Resilient to the in-loop reconnect path (gemini_session._receive_loop flips
    `connected` False->True on a mid-session error). A single transient error or
    a brief disconnect window must not permanently kill audio: we only exit if
    the session stays down past a short grace period (a real pause/close, which
    also cancels this task via _cancel_audio_tasks()).
    """
    try:
        while True:
            if not session.connected:
                disconnected_since = time.monotonic()
                while not session.connected:
                    await asyncio.sleep(0.2)
                    if time.monotonic() - disconnected_since > 3.0:
                        return
                continue
            try:
                pcm = await session.get_audio()
                await voice_bridge.send_audio(pcm)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("Gemini -> voice drain transient error; continuing", exc_info=True)
                await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("Gemini -> voice drain task error")


async def _watchdog_task(session: GeminiSession) -> None:
    """Pause Gemini session after IDLE_TIMEOUT_SEC of silence.

    Saves transcript context so the session resumes seamlessly
    when the user speaks again (handled by _on_voice_audio).

    Guarded against tearing down work the user is in the middle of:
    pause is deferred while any do_with_claude loop is in flight or a
    tier-X/I confirmation is pending. Without this guard the 25s idle
    timer would race a 60s confirmation window — the brain dies before
    the user can answer "yes," and the result gets orphaned to #ucs.
    """
    global _last_audio_received_at, _session_paused, _pause_transcript
    from .tools import has_in_flight_loops
    try:
        while True:
            await asyncio.sleep(5)
            if _last_audio_received_at == 0:
                continue
            idle = time.monotonic() - _last_audio_received_at
            if idle <= IDLE_TIMEOUT_SEC or not session.connected:
                continue
            if _pending_text_confirmations:
                log.info(
                    "%ds silence but %d tier-X/I confirmation(s) pending — "
                    "deferring Gemini pause",
                    IDLE_TIMEOUT_SEC, len(_pending_text_confirmations),
                )
                continue
            if has_in_flight_loops():
                log.info(
                    "%ds silence but agent loop in flight — deferring Gemini pause",
                    IDLE_TIMEOUT_SEC,
                )
                continue
            log.info("%ds silence — pausing Gemini session", IDLE_TIMEOUT_SEC)
            try:
                _pause_transcript = session.get_transcript_context(max_turns=10)
                await session.close()
            except Exception:
                log.exception("Error pausing Gemini session")
            _session_paused = True
            _last_audio_received_at = 0.0
            log.info("Gemini paused. Will auto-resume when user speaks.")
            return
    except asyncio.CancelledError:
        pass


def _cancel_audio_tasks() -> None:
    for task in _audio_tasks:
        if not task.done():
            task.cancel()
    _audio_tasks.clear()


async def _on_voice_exit_timeout() -> None:
    """Callback from VoiceController when the exit watchdog fires.

    Handles app-level cleanup (cancel audio tasks, close sessions, post
    alert). The controller handles bridge.leave() and state transition.
    """
    global _spicylit_active, _grok_session, _session_paused, _grok_paused
    global _pause_transcript
    _cancel_audio_tasks()

    _session_paused = False
    _grok_paused = False
    _pause_transcript = ""

    if _spicylit_active and _grok_session:
        _log_grok_cost()
        try:
            if _grok_session.connected:
                await _grok_session.close()
        except Exception:
            log.exception("Error closing Grok on voice exit")
        _grok_session = None
        _spicylit_active = False

    if gemini and gemini.connected:
        try:
            await gemini.close()
        except Exception:
            log.exception("Error closing Gemini on voice exit")

    try:
        await post_to_alerts(
            "Left voice channel after 10 minutes of silence."
        )
    except Exception:
        log.exception("Failed to post voice exit alert")


async def _auto_join_voice_channel(channel: discord.VoiceChannel) -> bool:
    """Reconcile voice to `channel` and bring up the right AI session for it.

    The single voice-presence entry point. Used by on_voice_state_update
    (user joins/moves), the on_ready post-preflight rescan, and the !join
    command. Picks the Grok/SpicyLit pipeline when the channel matches
    DISCORD_SPICYLIT_CHANNEL_ID, otherwise the Gemini/Aria pipeline.

    Returns True when voice + the AI pipeline are live in `channel`, False
    on any refusal (bridge down, transition busy, pipeline bring-up failed)
    so callers surface it instead of claiming success.
    """
    global _spicylit_active, _grok_session, _grok_session_started_at
    global _last_audio_received_at

    is_spicylit = (
        config.discord_spicylit_channel_id
        and str(channel.id) == config.discord_spicylit_channel_id
    )

    # Idempotent no-op only when already correctly connected in THIS
    # channel. The channel comparison is load-bearing: without it a lurking
    # Aria treats a join to any other channel as "already here" and never
    # follows the user out of the channel she is parked in.
    already_here = (
        voice_controller.in_voice
        and voice_controller.channel_id == str(channel.id)
    )
    if already_here:
        if is_spicylit and _spicylit_active and _grok_session and _grok_session.connected:
            log.info("SpicyLit already active in %s — nothing to reconcile", channel.name)
            return True
        if not is_spicylit and gemini and gemini.connected:
            log.info("Gemini already active in %s — nothing to reconcile", channel.name)
            return True

    if not voice_bridge.alive:
        log.warning("Asked to auto-join %s but voice bridge not alive", channel.name)
        return False

    if _local_session_active:
        await _close_local_session()
    if _wake_listener:
        _wake_listener.pause()

    # One presence primitive reconciles voice to this channel: fresh
    # connect, channel move, and lurk re-entry are the same call. Lurk is a
    # leave-time policy (we never depart when the user does); it does not
    # fork the join path. A move out of a lurked channel works because the
    # sidecar's doJoin tears down the old connection first.
    try:
        await voice_controller.ensure_in_channel(
            str(channel.id),
            audio_callback=_on_voice_audio,
            on_watchdog_expire=_on_voice_exit_timeout,
        )
    except VoiceTransitionBusy:
        log.warning(
            "Voice transition already in flight — not reconciling to %s", channel.name
        )
        return False
    log.info("Voice reconciled to %s", channel.name)

    _last_audio_received_at = 0.0
    _cancel_audio_tasks()

    if is_spicylit:
        if not config.grok_api_key:
            log.error("Joined #spicy-lit but GROK_API_KEY not set — no audio pipeline")
            return False

        if _grok_session and _grok_session.connected:
            log.warning("Closing leaked Grok session before creating new one")
            try:
                await _grok_session.close()
            except Exception:
                log.exception("Error closing leaked Grok session")

        if gemini and gemini.connected:
            try:
                await gemini.close()
            except Exception:
                log.exception("Error closing Gemini before Grok handoff")

        from capabilities.spicy_lit import GrokVoiceSession, init_table
        from capabilities.spicy_lit.prompts import STORY
        init_table()

        _grok_session = GrokVoiceSession(
            api_key=config.grok_api_key,
            voice="eve",
            user_id=config.authorized_user_ids[0] if config.authorized_user_ids else "",
            mode=STORY,
            post_text_callback=_post_to_spicylit,
            on_disconnect=_on_grok_disconnect,
        )
        await _grok_session.start()
        _spicylit_active = True
        _grok_session_started_at = time.monotonic()

        _audio_tasks.append(asyncio.create_task(_drain_grok_to_voice()))
        _audio_tasks.append(asyncio.create_task(_grok_watchdog_task()))
        log.info("Grok voice pipeline active for #spicy-lit (mode=%s)", STORY)
    else:
        if gemini and not gemini.connected:
            try:
                await gemini.connect()
            except Exception:
                log.exception("Failed to connect Gemini on auto-join")
                return False

        _audio_tasks.append(asyncio.create_task(_drain_gemini_to_voice(gemini)))
        _audio_tasks.append(asyncio.create_task(_watchdog_task(gemini)))

        if gemini and gemini.connected:
            try:
                buffer_ctx = conversation.as_gemini_injection(max_turns=10)
                briefed = await _inject_registry_briefing(on_resume=False)
                if not briefed:
                    await gemini.inject_text(
                        "[Context: Corbin just joined the voice channel. Stay silent until he speaks.]",
                        turn_complete=False,
                    )
                if buffer_ctx:
                    await gemini.inject_text(buffer_ctx, turn_complete=False)
            except Exception:
                log.exception("Failed to inject join context into Gemini")

    return True


def _find_authorized_user_voice_channel() -> discord.VoiceChannel | None:
    """Return the voice channel the authorized user is currently in, if any."""
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for m in vc.members:
                if str(m.id) in config.authorized_user_ids:
                    return vc
    return None


async def _voice_presence_reconcile_loop() -> None:
    """Slow self-healer that follows the user into voice after a gateway RESUME.

    `on_voice_state_update` only fires on a *transition*; a bare Discord
    gateway RESUME re-attaches to a session whose voice state predates the
    reconnect, so no join event is replayed and `on_ready` (which would run
    the rescan) does not re-fire either. The result is Aria sitting idle
    while the user waits in a channel. This loop closes that gap: on a slow
    cadence, if the authorized user is in a voice channel Aria is NOT in,
    route it through the single presence path so she follows.

    It only ever FOLLOWS — when the user is in no voice channel it does
    nothing, leaving lurk policy (the leave-time "don't depart" rule) to own
    presence. Steady state is a cheap membership scan with no side effects.
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await asyncio.sleep(_VOICE_RECONCILE_INTERVAL_SEC)
            if not _preflight_passed or not voice_bridge.alive:
                continue
            target = _find_authorized_user_voice_channel()
            if target is None:
                continue
            already_following = (
                voice_controller.in_voice
                and voice_controller.channel_id == str(target.id)
            )
            if already_following:
                continue
            log.info(
                "Voice reconciler: user in %s but Aria is %s — following",
                target.name,
                f"parked in channel {voice_controller.channel_id}"
                if voice_controller.in_voice
                else "not in voice",
            )
            await _auto_join_voice_channel(target)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Voice presence reconciler iteration failed — continuing")


async def _judge_sweep_loop() -> None:
    """Durable backstop for correctness judging.

    The inline judge was a fire-and-forget `asyncio.create_task` dropped on
    process churn, so the longest/failed sessions (worst-when-worst) went
    unjudged — ~45% loss. This sweeps the DB worklist on a slow cadence and
    judges any session_record that still has no verdict; it is idempotent via
    the LEFT JOIN in `get_unjudged_records`, so there is exactly one judging
    path and it survives restarts.
    """
    from .judge import sweep_unjudged
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await asyncio.sleep(_JUDGE_SWEEP_INTERVAL_SEC)
            await sweep_unjudged(hours=24, alert=post_to_alerts)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Judge sweep iteration failed — continuing")


# ---------------------------------------------------------------------------
# Cursor event consumer
# ---------------------------------------------------------------------------

def _summarize_sdk_event(etype: str, data: dict) -> str | None:
    """Render a single SDK event as a short thread-visible line.

    Returns None for events we don't surface (system init noise, raw
    audio, etc.). The wrapper passes `event.type` from `run.stream()`
    verbatim; the real vocabulary is `system / user / assistant /
    tool_call / thinking / status / request / task` plus the wrapper's
    synthetic `completion / error`. See cursor_wrapper/index.js.
    """
    if not isinstance(data, dict):
        return None

    if etype == "tool_call":
        name = data.get("name") or data.get("tool", "?")
        status = data.get("status", "")
        args = data.get("args") or {}
        hint = ""
        if isinstance(args, dict):
            for k in ("path", "command", "query", "file"):
                v = args.get(k)
                if isinstance(v, str):
                    hint = f" `{v[:80]}`"
                    break
        return f"`tool` {name} ({status}){hint}" if status else f"`tool` {name}{hint}"

    if etype == "assistant":
        message = data.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text", "")
                    if isinstance(t, str) and t.strip():
                        return f"`says` {t.strip()[:500]}"
        return None

    if etype == "thinking":
        return None

    if etype == "status":
        s = (data.get("status") or "").upper()
        if s:
            return f"`status` {s}"
        return None

    if etype == "task":
        sub = data.get("status") or data.get("subtype") or ""
        text = data.get("text") or ""
        return f"`task` {sub} {text[:200]}".strip()

    if etype == "system":
        sub = data.get("subtype") or ""
        if sub == "init":
            return None
        return f"`system` {sub}"

    if etype == "user":
        return None

    summary = data.get("summary") or data.get("text") or ""
    if not summary:
        return None
    return f"`{etype}` {str(summary)[:500]}"


async def _cursor_event_consumer(
    session_id: str, thread: discord.Thread | None
) -> None:
    """Read Cursor build events and route to Discord + Gemini.

    Consumes the real `@cursor/sdk` event vocabulary (tool_call, assistant,
    thinking, status, task) plus the two synthetic events the Node wrapper
    appends (completion, error). Posts to the build thread (throttled) and
    speaks completion / errors aloud if Aria is currently on voice.
    """
    last_post = 0.0
    throttle_sec = 5.0
    finished = False
    try:
        async for event in cursor_bridge.read_events(session_id):
            if _cancel_flag:
                break
            etype = event.get("event", "")
            data = event.get("data") or {}

            try:
                await cursor_registry.record_sdk_event(
                    session_id=session_id, event=etype, data=data
                )
            except Exception:
                log.exception(
                    "cursor_registry.record_sdk_event raised for sid=%s event=%s",
                    session_id, etype,
                )

            if etype == "completion" or (etype == "status" and isinstance(data, dict) and (data.get("status") or "").upper() == "FINISHED"):
                if not finished:
                    if thread:
                        try:
                            await thread.send("Build complete.")
                        except Exception:
                            log.exception("thread.send for completion failed")
                    if gemini and gemini.connected:
                        try:
                            await gemini.inject_text(
                                "The Cursor build has completed successfully.", turn_complete=True
                            )
                        except Exception:
                            log.exception("gemini inject for completion failed")
                    upsert_cursor_session(session_id, "", status="completed")
                    finished = True
                if etype == "completion":
                    break
                continue

            if etype == "error" or (etype == "status" and isinstance(data, dict) and (data.get("status") or "").upper() in ("ERROR", "EXPIRED", "CANCELLED")):
                msg = data.get("message") or data.get("status") or "Unknown error"
                try:
                    await post_to_alerts(f"Cursor error ({session_id[:8]}): {msg}")
                except Exception:
                    log.exception("alerts post for error failed")
                upsert_cursor_session(session_id, "", status="error")
                if etype == "error":
                    break
                continue

            line = _summarize_sdk_event(etype, data)
            if line:
                now = time.monotonic()
                if now - last_post > throttle_sec and thread:
                    try:
                        # Incremental progress: silent. Only "Build complete."
                        # (above) and explicit error alerts should ding. Without
                        # this, an active build floods the thread with dings.
                        await thread.send(line, silent=True)
                    except Exception:
                        log.exception("thread.send for SDK event failed")
                    last_post = now
                    update_cursor_session_event(session_id, etype)
    except Exception:
        log.exception("Cursor event consumer error for %s", session_id)
    finally:
        cursor_bridge.close_session(session_id)


# ---------------------------------------------------------------------------
# Registry-driven narrator (replaces the legacy _cursor_external_pager)
#
# One callback (`_narrate_registry_event`) owns the cross-cutting decisions
# for every cursor event: idle-gated speech inject, DM rung, conversation
# buffer record, silent #ucs-alerts audit. The `CursorAgentRegistry` calls
# this whenever an agent transitions to a state worth surfacing.
# ---------------------------------------------------------------------------


async def _dm_authorized_user(content: str) -> bool:
    """Send a DM to the first authorized user. Returns True on success,
    False on any delivery failure (Forbidden, HTTPException, missing user).

    Failures are logged but NOT individually posted to #ucs-alerts. The
    caller owns the "escalate to a loud alerts post" decision so we get
    exactly one Discord message per event instead of the double-post
    pattern (silent audit + loud "Pager DM blocked" wall-of-text) that
    flooded #ucs-alerts with two records per Cursor hook.
    """
    if not config.authorized_user_ids:
        log.warning("No authorized_user_ids configured — cannot DM")
        return False
    user_id = int(config.authorized_user_ids[0])
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except discord.HTTPException:
        log.exception("Could not fetch authorized user for DM")
        return False

    try:
        dm = user.dm_channel or await user.create_dm()
    except discord.HTTPException:
        log.exception("create_dm failed for %s", user_id)
        return False

    for chunk in _split_at_paragraphs(content):
        try:
            await dm.send(chunk)
        except discord.Forbidden:
            log.warning(
                "User %s has DMs disabled — caller will escalate to #ucs-alerts if severity warrants",
                user_id,
            )
            return False
        except discord.HTTPException:
            log.exception("DM send failed")
            return False
    return True


def _cached_thread_label(sid: str) -> str:
    """Distilled human label for a thread sid from the summaries cache, or ''.

    Best-effort enrichment so concurrent threads in one workspace narrate
    with distinct names instead of the identical `project_label`. A cache
    miss (thread not distilled yet) is the normal case and returns ''. A
    real DB error is logged loudly but must never break narration.
    """
    if not sid:
        return ""
    try:
        from .db import get_thread_summary
        row = get_thread_summary(sid)
    except Exception:
        log.exception("thread-summary cache lookup failed for sid=%s", sid[:8])
        return ""
    if not row:
        return ""
    return str(row.get("label") or "").strip()


def _format_registry_context_for_inject(evt: RegistryEvent) -> str:
    """Structured silent-context block for Gemini before the heads-up trigger.

    Mirrors the shape the previous `_format_event_context` produced, but
    sourced from the registry's `CursorAgent` instead of the now-deleted
    `CursorEvent`. Includes the canonical `agent_id` Aria's tools take.
    """
    agent = evt.agent
    lines = ["[Cursor watch context for the event you are about to narrate:"]
    lines.append(f"  agent_id: {agent.agent_id}")
    lines.append(f"  workspace_root: {agent.workspace_root}")
    lines.append(f"  project_label: {agent.project_label}")
    if agent.current_sid:
        lines.append(f"  thread_handle: {agent.project_label}/{agent.current_sid[:8]}")
        thread_label = _cached_thread_label(agent.current_sid)
        if thread_label:
            lines.append(f"  thread_label: {thread_label}")
    lines.append(f"  source: {agent.source}")
    lines.append(f"  status: {agent.status}")
    lines.append(f"  kind: {evt.kind}")
    lines.append(f"  severity: {evt.severity}")
    lines.append(f"  reason: {evt.reason}")
    if agent.pending_question:
        lines.append(f"  pending_question: {agent.pending_question[:400]}")
    if agent.last_assistant_text:
        snippet = agent.last_assistant_text[:600].replace("\n", " ")
        lines.append(f"  last_assistant_text: {snippet}")
    if agent.recent_plan_files:
        lines.append(
            "  recent_plan_files: " + ", ".join(agent.recent_plan_files[-3:])
        )
    lines.append(
        "  For follow-ups: read this exact thread with "
        "cursor_read(thread_handle above), or cursor_send to it. For the full "
        "picture of every thread in this project, call cursor_threads. "
        "(cursor_send kind=chat/new_agent/approve/reject/cancel).]"
    )
    return "\n".join(lines)


def _format_registry_speech(evt: RegistryEvent) -> str:
    """Concise spoken line for the heads-up trigger."""
    agent = evt.agent
    # Differentiate concurrent threads at the narration seam: lead with the
    # thread's distilled label (never its UUID) so two threads in one project
    # don't both narrate the identical "...in live_visuals_4" line.
    label = _cached_thread_label(agent.current_sid) if agent.current_sid else ""
    headline = f"{label}: {evt.reason}" if label else evt.reason
    parts: list[str] = [headline]
    if agent.pending_question:
        parts.append(f"It asked: {agent.pending_question[:240]}")
    elif agent.last_assistant_text and evt.kind in ("finished", "errored", "progress"):
        parts.append(f"Last thing it said: {agent.last_assistant_text[:240]}")
    if agent.recent_plan_files and evt.kind == "started":
        parts.append(
            f"Plan files: {', '.join(agent.recent_plan_files[-2:])}."
        )
    return " ".join(p for p in parts if p)


def _format_registry_dm(evt: RegistryEvent) -> str:
    """One-shot Discord DM body. Mention-prefixed so the phone buzzes."""
    agent = evt.agent
    mention_id = config.authorized_user_ids[0] if config.authorized_user_ids else ""
    mention = f"<@{mention_id}> " if mention_id else ""
    project = (agent.workspace_root or "").rstrip("/").split("/")[-1] or agent.workspace_root
    lines = [f"{mention}{evt.reason} ({project})"]
    if agent.pending_question:
        lines.append(f"It's asking: {agent.pending_question[:300]}")
    elif agent.last_assistant_text:
        snippet = agent.last_assistant_text[:300].replace("\n", " ")
        lines.append(f"Latest: {snippet}")
    # Decision-oriented tail: tell Corbin what he can actually do about it,
    # rather than a generic "join voice". Context > noise.
    if agent.pending_question:
        lines.append("\u2192 Reply here with the answer and I'll relay it, or say it in voice.")
    elif evt.kind in ("error", "errored", "failed"):
        lines.append("\u2192 Looks stuck. Reply 'fix it' and I'll investigate + propose an approach to approve.")
    else:
        lines.append("\u2192 Reply for a 1-line debrief, or ignore \u2014 I'll keep watching and only ping when it matters.")
    return "\n".join(lines)


# Rate-limit auto-proposals so a burst of watched windows finishing at once
# can't fire a wall of decision buzzes. One auto-proposal per project per window.
_last_proposal_at: dict[str, float] = {}
_AUTO_PROPOSAL_COOLDOWN_SEC = 600.0


async def _maybe_propose_next_after_completion(agent: "CursorAgent") -> bool:
    """A watched thread finished. Instead of a useless 'it's done' buzz, ask
    Claude for the single best next action and push it as a decision (which
    buzzes). Returns True if a proposal was posted. Rate-limited per project.
    """
    if not config.propose_next_on_completion:
        return False
    project = (agent.workspace_root or "").rstrip("/").split("/")[-1] or "project"
    now = time.monotonic()
    if now - _last_proposal_at.get(project, 0.0) < _AUTO_PROPOSAL_COOLDOWN_SEC:
        return False
    summary = (agent.last_assistant_text or agent.pending_question or "").strip()
    if not summary:
        return False
    try:
        from .tools import suggest_next_action
        nxt = await suggest_next_action(project, summary)
    except Exception:
        log.exception("suggest_next_action raised")
        return False
    if not nxt:
        return False
    _last_proposal_at[project] = now
    try:
        await _propose_action(
            title=f"Next step on {project}",
            why=f"That thread just finished: {_clip(summary, 600)}",
            task=nxt,
            session_key="",
        )
        return True
    except Exception:
        log.exception("propose-next failed for %s", project)
        return False


async def _narrate_registry_event(evt: RegistryEvent) -> None:
    """Single owner of voice / DM / alert routing for cursor registry events.

    Replaces the four-rung `_cursor_external_pager`. Order is mechanical:

    1. Record the cursor event in the conversation buffer (always).
    2. Silent audit to `#ucs-alerts` (always).
    3. On voice + idle gate: structured context inject + speech trigger
       when severity warrants. The wait_until_idle gate prevents the
       narration from being batched into Aria's in-flight turn.
    4. Not on voice + high severity: DM the authorized user.
    5. Mark `agent.last_delivered_at` on success of step 3 or 4 so the
       join / resume briefing doesn't re-narrate.

    The legacy "rung B/C/D" semantics collapse: a single high-severity
    event whose DM fails posts a loud audit. Everything else is silent.
    """
    agent = evt.agent
    log.info(
        "Registry event: kind=%s severity=%s agent=%s source=%s",
        evt.kind, evt.severity, agent.agent_id, agent.source,
    )

    audit_line = (
        f"**[Cursor watch] {evt.reason}** "
        f"_(severity={evt.severity}, kind={evt.kind}, source={agent.source})_"
    )

    try:
        conversation.add_cursor_event(_format_registry_context_for_inject(evt))
    except Exception:
        log.exception("Failed to record cursor event in conversation buffer")

    async def _safe_post(content: str, *, silent: bool) -> None:
        try:
            await post_to_alerts(content, silent=silent)
        except Exception:
            log.exception("Failed to post cursor watch audit")

    on_voice = bool(gemini and gemini.connected)
    if on_voice:
        try:
            try:
                idle = await gemini.wait_until_idle(timeout=15.0)
                if not idle:
                    log.warning(
                        "Registry narrate inject timed out waiting for Gemini idle — "
                        "proceeding (may be batched into an in-flight turn)"
                    )
            except Exception:
                log.exception(
                    "wait_until_idle raised on registry narrate — proceeding with inject"
                )
            try:
                await gemini.inject_text(
                    _format_registry_context_for_inject(evt), turn_complete=False
                )
            except Exception:
                log.exception(
                    "Failed to inject structured registry context — "
                    "continuing to the heads-up trigger anyway"
                )
            spoke_aloud = False
            if evt.severity == "high":
                speech = _format_registry_speech(evt)
                await gemini.inject_text(
                    f"[Cursor watch heads-up — narrate this and ask what to do next] {speech}",
                    turn_complete=True,
                )
                spoke_aloud = True
            if spoke_aloud:
                # Only advance the delivery watermark when Aria actually
                # said something out loud. Silent injects (low severity)
                # are absorbed into her live context but do not count as
                # "delivered" — if her session closes before the user
                # debriefs, the briefing on rejoin still surfaces the
                # event.
                agent.last_delivered_at = agent.last_event_at
                agent.last_delivered_reason = evt.reason
            await _safe_post(audit_line, silent=True)
            return
        except Exception:
            log.exception(
                "Failed to inject registry event into Gemini — falling back to DM rung"
            )

    # Not on voice. The ONLY thing allowed to buzz is a real decision. A bare
    # "thread finished" is useless on its own (Corbin's rule), so on completion
    # we turn it into a "here's the next move — approve?" proposal (which buzzes
    # the main channel). Everything else just streams silently to #ucs-alerts.
    if evt.severity == "high" and evt.kind in ("finished", "completed"):
        proposed = await _maybe_propose_next_after_completion(agent)
        if not proposed:
            await _safe_post(audit_line, silent=True)
    else:
        # Errors, questions, progress: context to the silent stream, no buzz.
        # (A pending question is surfaced here; the proposal path is the buzz.)
        body = _format_registry_dm(evt) if evt.severity == "high" else audit_line
        await _safe_post(body, silent=True)


def _undelivered_agents() -> list[CursorAgent]:
    """Agents whose latest event hasn't been narrated yet.

    Replaces the `_pending_pages` deque. The registry is the source of
    truth; each agent carries its own `last_delivered_at` watermark that
    the narrator advances when speech or DM lands.
    """
    out = [
        a for a in cursor_registry.agents()
        if a.last_event_at > 0 and a.last_event_at > a.last_delivered_at
    ]
    out.sort(key=lambda a: a.last_event_at, reverse=True)
    return out


def _format_undelivered_briefing(agents: list[CursorAgent]) -> str:
    """Render undelivered agents as a join / resume briefing.

    Prefers `last_event_reason` (set by every emit, including DM-only
    paths) over `last_delivered_reason` (set only when Aria actually
    spoke). This way a Cursor task that completed while Corbin was on
    his phone gets surfaced as "Task completed in X" on his next voice
    join, not the stale reason from the last time Aria spoke about that
    agent.
    """
    lines: list[str] = []
    for agent in agents:
        reason = (
            agent.last_event_reason
            or agent.last_delivered_reason
            or "recent activity"
        )
        bullet = (
            f"- {agent.project_label} ({agent.source}, status={agent.status}): "
            f"{reason}"
        )
        bullet += f"\n    agent_id: {agent.agent_id}"
        if agent.workspace_root and agent.workspace_root != agent.agent_id:
            bullet += f"\n    workspace_root: {agent.workspace_root}"
        if agent.pending_question:
            bullet += f"\n    pending_question: {agent.pending_question[:300]}"
        elif agent.last_assistant_text:
            snippet = agent.last_assistant_text[:300].replace("\n", " ")
            bullet += f"\n    last said: {snippet}"
        if agent.recent_plan_files:
            bullet += (
                "\n    recent_plan_files: "
                + ", ".join(agent.recent_plan_files[-2:])
            )
        lines.append(bullet)
    return "\n".join(lines)


async def _inject_registry_briefing(*, on_resume: bool = False) -> bool:
    """Inject a briefing covering undelivered cursor activity.

    Replaces `_inject_pending_pages_briefing`. Reads agent state straight
    from `cursor_registry`; marks each surfaced agent as delivered after
    a successful inject so it isn't re-briefed on the next pause/resume.

    `on_resume=True` is for the pause/resume branch in `_on_voice_audio`;
    `on_resume=False` is the fresh-join preamble in
    `_auto_join_voice_channel`.
    """
    if not gemini or not gemini.connected:
        return False
    agents = _undelivered_agents()
    if not agents:
        return False
    briefing = _format_undelivered_briefing(agents)
    if on_resume:
        preamble = (
            "[Context: while you were paused, the following Cursor agents had "
            "activity. When Corbin speaks next, OPEN with a one-sentence briefing "
            "covering them and then ask what he wants to do next:\n"
            f"{briefing}]"
        )
    else:
        preamble = (
            "[Context: Corbin just joined voice. While he was away, these Cursor "
            "agents had activity. When he speaks, OPEN with a one-sentence "
            "briefing covering them and then ask what he wants to do next:\n"
            f"{briefing}]"
        )
    try:
        await gemini.inject_text(preamble, turn_complete=False)
    except Exception:
        log.exception(
            "Failed to inject registry briefing (on_resume=%s)", on_resume
        )
        return False
    for agent in agents:
        agent.last_delivered_at = max(agent.last_delivered_at, agent.last_event_at)
    return True


# ---------------------------------------------------------------------------
# Bot events and commands
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    global gemini, _preflight_passed, _last_preflight_report
    global _pending_voice_channel_id
    global _on_ready_done
    global cursor_observer
    global _voice_reconcile_task
    global _judge_sweep_task
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)

    if not _on_ready_done:
        init_db()
        init_memory()

        async def _gemini_reconnect() -> None:
            if gemini:
                await gemini.reconnect()

        from .tools import init_tools
        init_tools(
            cursor_bridge=cursor_bridge,
            post_callback=post_to_text,
            alert_callback=post_to_alerts,
            thread_callback=create_build_thread,
            cursor_event_callback=_cursor_event_consumer,
            reconnect_callback=_gemini_reconnect,
            discord_history_callback=_fetch_discord_history,
            discord_threads_callback=_fetch_discord_threads,
            progress_callback=_emit_progress_to_user,
            propose_callback=_propose_action,
        )

        await cursor_bridge.start()

        try:
            await voice_bridge.start()
        except Exception:
            log.exception("voice bridge failed to start — !join will not work")

        async def _on_orphan_tool_result(tool_name: str, fc_id: str, result: str) -> None:
            """Loud failure for L1: tool ran but session closed before response sent."""
            preview = result[:500].replace("\n", " ")
            try:
                await post_to_alerts(
                    f"**ORPHAN TOOL RESULT** `{tool_name}` (id={fc_id[:12]}) — "
                    f"the tool finished but the Gemini session closed before the "
                    f"result could be returned to the model. "
                    f"Side effects may have completed. Preview: `{preview}`"
                )
            except Exception:
                log.exception("Failed to post orphan-tool alert")

        gemini = GeminiSession(
            tool_handler=_handle_tool_call,
            transcript_callback=_on_voice_transcript,
            orphan_callback=_on_orphan_tool_result,
        )

        from .tools import set_transcript_provider
        set_transcript_provider(lambda: gemini.get_recent_transcript(3) if gemini else [])

        from .mcp import init_mcp
        mcp = None
        try:
            mcp = await init_mcp()
            log.info("MCP fleet started")

            async def _confirm_callback(action_id: str, tool_name: str, summary: str) -> dict:
                # Register a text-side waiter before posting the card so
                # !ok/!no and reactions can resolve it even if Gemini is
                # disconnected (text-channel-initiated do_with_claude task).
                _register_pending_confirmation(action_id, tool_name, summary)
                try:
                    card_message = await _post_confirmation_card(
                        action_id, tool_name, summary
                    )
                    if card_message is not None:
                        pending = _pending_text_confirmations.get(action_id)
                        if pending is not None:
                            pending.message_id = card_message.id
                            _confirmation_message_index[card_message.id] = action_id
                    if gemini and gemini.connected:
                        try:
                            await gemini.inject_text(
                                f"I need your approval. About to run: {tool_name}. "
                                f"Details: {summary}. Do you approve? Say yes or no. "
                                f"When you call confirm_action, pass "
                                f"action_id=\"{action_id}\".",
                                turn_complete=True,
                            )
                        except Exception:
                            log.exception("Failed to inject confirmation prompt to Gemini")
                    return await _race_confirmations(
                        gemini.wait_for_confirmation(
                            action_id, timeout=CONFIRM_TIMEOUT_SEC
                        ),
                        _discord_wait_for_confirmation(
                            action_id, timeout=CONFIRM_TIMEOUT_SEC
                        ),
                    )
                finally:
                    _unregister_pending_confirmation(action_id)

            mcp.set_confirm_callback(_confirm_callback)
        except Exception:
            log.exception("MCP fleet failed to start — preflight will flag MCP probes")

        def _voice_injector_when_connected():
            """Return the Gemini session for /aria_say when connected, else None."""
            return gemini if (gemini and gemini.connected) else None

        try:
            from . import tools as tools_module
            cursor_registry.set_project_aliases(dict(tools_module.PROJECT_REGISTRY))
            cursor_registry.set_emit_callback(_narrate_registry_event)
            cursor_observer = CursorExternalObserver(
                registry_provider=lambda: dict(tools_module.PROJECT_REGISTRY),
                voice_injector_provider=_voice_injector_when_connected,
                registry_writer=cursor_registry.register_from_hook,
                conversation_provider=lambda: conversation,
                gemini_provider=lambda: gemini,
            )
            await cursor_observer.start()
        except OSError as exc:
            log.exception(
                "External Cursor observer failed to bind %s:%s — port in use?",
                config.cursor_event_host, config.cursor_event_port,
            )
            cursor_observer = None
            try:
                await post_to_alerts(
                    f"**External Cursor observer failed to start:** {exc}. "
                    "Other-window watching is disabled this boot."
                )
            except Exception:
                pass
        except Exception:
            log.exception("External Cursor observer failed to start")
            cursor_observer = None

        from .preflight import run_all, format_report, format_summary
        report = await run_all(
            mcp_client=mcp,
            cursor_bridge=cursor_bridge,
            cursor_observer=cursor_observer,
            alert_callback=post_to_alerts,
            include_gemini=True,
            include_cursor=True,
        )
        _last_preflight_report = report
        _preflight_passed = report.ok

        # Quiet-when-healthy: the full per-probe report is a wall of text that
        # spammed #ucs-alerts on every launch. Post the wall only when a
        # CRITICAL probe failed (the user must act); otherwise a one-line
        # summary. The full report always goes to the logs.
        formatted = format_report(report, markdown=True)
        log.info("Preflight report:\n%s", formatted)
        try:
            if report.ok:
                await post_to_alerts(format_summary(report, markdown=True), silent=True)
            else:
                await post_to_alerts(formatted)
        except Exception:
            log.exception("Failed to post preflight report to alerts channel")

        if not report.ok:
            log.error(
                "PREFLIGHT FAILED: %d critical / %d warnings. Bot will NOT accept !join.",
                len(report.critical_failures), len(report.warnings),
            )
            for r in report.critical_failures:
                log.error("  [CRIT] %s: %s | fix: %s", r.name, r.error, r.fix_command)
            return

        log.info("Preflight passed (%d probes). Gemini will connect on !join.", len(report.results))

        global _wake_listener
        if _wake_listener is not None:
            try:
                _wake_listener.stop()
            except Exception:
                pass
        try:
            from .wake_word import WakeWordListener
            _wake_listener = WakeWordListener(on_wake=_on_wake_word)
            await _wake_listener.start()
            log.info("Wake-word listener active")
        except Exception:
            log.exception("Wake-word listener failed to start — local voice unavailable")
            _wake_listener = None

        # Self-heal voice presence after bare gateway RESUMEs that skip the
        # on_ready rescan. Started once here in boot-only init so a Discord
        # WS resume (which re-fires on_ready into the else branch) can't spawn
        # a duplicate.
        if _voice_reconcile_task is None or _voice_reconcile_task.done():
            _voice_reconcile_task = asyncio.create_task(
                _voice_presence_reconcile_loop(), name="voice_presence_reconcile"
            )

        # Durable correctness judging — replaces the dropped inline judge task.
        # Guarded the same way so a WS resume can't spawn a duplicate.
        if _judge_sweep_task is None or _judge_sweep_task.done():
            _judge_sweep_task = asyncio.create_task(
                _judge_sweep_loop(), name="judge_sweep"
            )

        _on_ready_done = True
    else:
        log.info("on_ready re-fired (Discord WS resume) — skipping boot-only init")

    target_channel: discord.VoiceChannel | None = None

    if _pending_voice_channel_id:
        ch = bot.get_channel(int(_pending_voice_channel_id))
        if isinstance(ch, discord.VoiceChannel):
            still_present = any(
                str(m.id) in config.authorized_user_ids for m in ch.members
            )
            if still_present:
                target_channel = ch
                log.info("Picking up deferred voice join for %s", ch.name)
        _pending_voice_channel_id = None

    if target_channel is None:
        target_channel = _find_authorized_user_voice_channel()
        if target_channel is not None:
            log.info("User already in %s at startup — auto-joining", target_channel.name)

    if target_channel is not None:
        await _auto_join_voice_channel(target_channel)


@bot.command()
async def join(ctx: commands.Context):
    """Join the authorized user's voice channel via the single presence path.

    Thin wrapper: all the connect/move/reconnect logic lives in
    `_auto_join_voice_channel` so manual `!join` and automatic join can
    never diverge.
    """
    if not _is_authorized(ctx.author.id):
        await ctx.send("Not authorized.")
        return
    if not _preflight_passed:
        await ctx.send(
            "Preflight failed — refusing to join voice. See #ucs-alerts for the failure report."
        )
        return
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("You're not in a voice channel.")
        return
    if not voice_bridge.alive:
        await ctx.send("Voice bridge not running — check DISCORD_VOICE_BOT_TOKEN.")
        return

    channel = ctx.author.voice.channel
    if (
        voice_controller.in_voice
        and voice_controller.channel_id == str(channel.id)
        and gemini and gemini.connected
    ):
        await ctx.send("Already in voice.")
        return

    if await _auto_join_voice_channel(channel):
        await ctx.send(f"Joined {channel.name}")
    else:
        await ctx.send(
            "Couldn't join — a voice transition is in progress or the bridge is down. "
            "Check #ucs-alerts."
        )


@bot.command()
async def leave(ctx: commands.Context):
    """Leave voice channel and close Gemini session."""
    if not _is_authorized(ctx.author.id):
        await ctx.send("Not authorized.")
        return
    _cancel_audio_tasks()
    if gemini and gemini.connected:
        await gemini.close()
    await voice_controller.leave()
    await ctx.send("Left voice channel.")


@bot.command()
async def stop(ctx: commands.Context):
    """Emergency stop: cancel all running tasks."""
    global _cancel_flag
    if not _is_authorized(ctx.author.id):
        await ctx.send("Not authorized.")
        return
    _cancel_flag = True
    from .tools import set_cancel_flag
    set_cancel_flag(True)
    await post_to_alerts("**!stop** — All tasks aborted by user.")
    await ctx.send("All tasks aborted.")


@bot.command(name="ok")
async def confirm_ok(ctx: commands.Context, action_id: str | None = None):
    """Approve a pending tier-X/I action by id, or the only one pending.

    Usage:
      !ok                       — approve when exactly one is pending
      !ok <action_id>           — approve a specific pending action
    """
    if not _is_authorized(ctx.author.id):
        await ctx.send("Not authorized.")
        return
    await _handle_text_confirmation(ctx, action_id, approved=True)


@bot.command(name="no")
async def confirm_no(ctx: commands.Context, action_id: str | None = None):
    """Decline a pending tier-X/I action by id, or the only one pending."""
    if not _is_authorized(ctx.author.id):
        await ctx.send("Not authorized.")
        return
    await _handle_text_confirmation(ctx, action_id, approved=False)


async def _handle_text_confirmation(
    ctx: commands.Context, action_id: str | None, approved: bool
) -> None:
    """Resolve a pending confirmation from a !ok / !no command."""
    pending_ids = list(_pending_text_confirmations)
    if not pending_ids:
        await ctx.send("Nothing's waiting for your okay right now.")
        return
    if action_id is None:
        if len(pending_ids) > 1:
            ids = ", ".join(f"`{i}`" for i in pending_ids)
            await ctx.send(
                f"A few things are waiting — tell me which one: {ids}"
            )
            return
        action_id = pending_ids[0]
    if not _resolve_pending_confirmation(action_id, approved, source="text"):
        await ctx.send("I couldn't find that one waiting for approval.")
        return
    if approved:
        await ctx.send("Got it \u2014 on it now. I'll show each step as I go.")
    else:
        await ctx.send("Okay \u2014 skipping that one.")


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    """Resolve a pending confirmation when the authorized user reacts.

    Watches messages tracked in `_confirmation_message_index`. A check
    or thumbs-up approves; a cross or thumbs-down declines. Reactions
    from anyone other than the authorized user are ignored.
    """
    if user.bot:
        return
    if not _is_authorized(user.id):
        return
    msg = reaction.message
    action_id = _confirmation_message_index.get(msg.id)
    if not action_id:
        return
    emoji = str(reaction.emoji)
    # Subtle, instant receipt right where Corbin tapped so he's never left
    # wondering whether Aria heard him. Silent so it doesn't double-buzz.
    if emoji in CONFIRM_APPROVE_EMOJIS:
        if _resolve_pending_confirmation(action_id, True, source="reaction"):
            try:
                await msg.channel.send(
                    "Got it \u2014 on it now. I'll show each step as I go.", silent=True
                )
            except Exception:
                log.debug("approve receipt failed (non-fatal)", exc_info=True)
    elif emoji in CONFIRM_DECLINE_EMOJIS:
        if _resolve_pending_confirmation(action_id, False, source="reaction"):
            try:
                await msg.channel.send("Okay \u2014 skipping that one.", silent=True)
            except Exception:
                log.debug("decline receipt failed (non-fatal)", exc_info=True)


@bot.command()
async def status(ctx: commands.Context):
    """Check system health and active sessions."""
    from .tools import handle_tool_call as htc
    parts: list[str] = []

    preflight_status = "PASSED" if _preflight_passed else "FAILED — bot is not ready"
    parts.append(f"**Preflight:** {preflight_status}")
    if _last_preflight_report is not None:
        from .preflight import PreflightReport
        rep: PreflightReport = _last_preflight_report  # type: ignore[assignment]
        parts.append(
            f"**Last preflight:** {len(rep.passed)}/{len(rep.results)} passed, "
            f"{len(rep.critical_failures)} critical, {len(rep.warnings)} warnings"
        )

    parts.append(f"**Gemini:** {'connected' if gemini and gemini.connected else 'disconnected'}")

    parts.append(f"**Cursor bridge:** {'alive' if cursor_bridge.alive else 'dead'}")

    spend = get_daily_spend()
    cap = config.daily_spend_cap_usd
    parts.append(f"**Daily spend:** ${spend:.2f} / ${cap:.2f}")

    sessions_json = await htc("cursor_status", {})
    parts.append(f"**Cursor sessions:** {sessions_json}")

    try:
        from .mcp import mcp_client
        if mcp_client:
            health = await mcp_client.health_check()
            parts.append(f"**MCP servers:** {health}")
        else:
            parts.append("**MCP servers:** not initialized")
    except ImportError:
        parts.append("**MCP servers:** module not loaded")

    try:
        from .db import get_correctness_summary
        summary = get_correctness_summary(hours=24)
        if summary:
            lines = ["**Correctness (24h):**"]
            for product, stats in sorted(summary.items()):
                rate = stats["correctness_rate"]
                total = stats["total"]
                lines.append(
                    f"  {product}: {rate:.0%} ({stats['correct']}/{total} correct, "
                    f"{stats['failed']} failed)"
                )
            parts.append("\n".join(lines))
        else:
            parts.append("**Correctness:** no verdicts in last 24h")
    except Exception:
        parts.append("**Correctness:** unavailable")

    await ctx.send("\n".join(parts))


@bot.command()
async def preflight(ctx: commands.Context):
    """Re-run preflight probes on demand."""
    global _preflight_passed, _last_preflight_report
    if not _is_authorized(ctx.author.id):
        await ctx.send("Not authorized.")
        return

    from .mcp import mcp_client
    from .preflight import run_all, format_report

    await ctx.send("Running preflight...")
    report = await run_all(
        mcp_client=mcp_client,
        cursor_bridge=cursor_bridge,
        cursor_observer=cursor_observer,
        alert_callback=post_to_alerts,
        include_gemini=True,
        include_cursor=True,
    )
    _last_preflight_report = report
    _preflight_passed = report.ok

    formatted = format_report(report, markdown=True)
    await post_to_alerts(formatted)
    await ctx.send(
        f"Preflight {'PASSED' if report.ok else 'FAILED'}: "
        f"{len(report.passed)}/{len(report.results)} passed. See #ucs-alerts."
    )


@bot.command()
async def reload(ctx: commands.Context):
    """Reload all prompt templates and reconnect Gemini session."""
    if not _is_authorized(ctx.author.id):
        await ctx.send("Not authorized.")
        return
    from .prompts import clear_cache
    clear_cache()
    if gemini and gemini.connected:
        await ctx.send("Reloading prompts and reconnecting Gemini...")
        await gemini.reconnect()
        await ctx.send("Prompts reloaded. Gemini session reconnected.")
    else:
        await ctx.send("Prompts reloaded. Gemini not connected — changes will apply on next !join.")


@bot.command()
async def ask(ctx: commands.Context, *, message: str):
    """Send a text request through the full tool dispatch (do_with_claude).

    Bypasses voice — same pipeline, text transport. Results post to the channel.
    Usage: !ask summarize my emails from today
    """
    if not _is_authorized(ctx.author.id):
        await ctx.send("Not authorized.")
        return
    await _run_ask(ctx.channel, message)


# ---------------------------------------------------------------------------
# Request threads — one Discord thread per request (the isolation primitive)
# ---------------------------------------------------------------------------

_THREAD_NAME_MAX = 90  # Discord caps thread names at 100; leave headroom.


def _thread_title(text: str) -> str:
    """A short, human thread name distilled mechanically from the request.

    No model call: the request text, whitespace-collapsed and capped. Cheap
    and deterministic, so opening a thread never adds latency or cost.
    """
    flat = " ".join((text or "").strip().split())
    return flat[:_THREAD_NAME_MAX] if flat else "Request"


async def _ensure_work_thread(channel, title_seed: str, anchor=None):
    """Resolve the Discord thread a request runs in. Returns (target, session_key).

    The dysfunctional primitive this fixes: a request used to be identified
    by its CHANNEL, so every #ucs message shared one agent lock and one
    context window (judged failure 144: "an agent loop is already running").
    Now a request is identified by its own thread:

    - already inside a thread     -> that thread is the session (a follow-up).
    - top-level in a text channel -> open a thread (anchored to the opener
      message when we have one, so the opener stays in #ucs as a clean index
      entry and the work happens in the thread).
    - a DM / non-threadable place  -> the channel itself is the session.
      Discord has no threads there; this is the correct medium, not a silent
      fallback.

    `session_key` is the thread id, so two requests can never collide. The
    binding is persisted (discord_threads) so a follow-up in an old thread
    still resolves to the same isolated session after a restart. A
    thread-create failure is raised to the caller, never swallowed.
    """
    if isinstance(channel, discord.Thread):
        sk = str(channel.id)
        bind_thread(sk, sk)
        return channel, sk
    if isinstance(channel, discord.TextChannel):
        name = _thread_title(title_seed)
        if anchor is not None:
            thread = await anchor.create_thread(name=name)
        else:
            thread = await channel.create_thread(
                name=name, type=discord.ChannelType.public_thread
            )
        sk = str(thread.id)
        bind_thread(sk, sk)
        return thread, sk
    return channel, str(getattr(channel, "id", "") or "")


async def _run_ask(channel, message: str) -> None:
    """Shared implementation for !ask — works for both commands and webhook messages.

    The *programmatic* entry point (iOS Shortcuts, webhook tests, automated
    callers). It opens its own request thread so each !ask is isolated and
    self-contained — ack, live steps, and the result all land in that one
    thread. It is a clean slate: no prior conversation context is prepended
    (a fresh thread has none anyway), so the same !ask reproduces the same
    agent loop regardless of what else was said.
    """
    target, session_key = await _ensure_work_thread(channel, message)
    channel_name = f"#{getattr(target, 'name', getattr(target, 'id', ''))}"
    conversation.add_user_text(channel=channel_name, text=message, session_key=session_key)
    # Silent placeholder so the user doesn't get a ding for an ack; the real
    # answer below is what should notify.
    await target.send(
        "Working on it — I'll show each step here, then the result. "
        "Any approval prompt lands in #ucs-alerts (reply `!ok <action_id>`).",
        silent=True,
    )
    from .tools import handle_tool_call
    try:
        result = await handle_tool_call("do_with_claude", {
            "task": message,
            "session_key": session_key,
        })
        # P4: never leak control-plane errors to the user. Log raw, send friendly.
        if _looks_like_control_plane_error(result):
            log.error("control-plane error quarantined (!ask): %s", result[:500])
            await target.send(_CONTROL_PLANE_FRIENDLY)
            return
        await _send_chunked(target, result)
        conversation.add_aria_text(channel=channel_name, text=result, session_key=session_key)
    except Exception:
        log.exception("_run_ask raised")
        await target.send(_CONTROL_PLANE_FRIENDLY)


def _augment_with_context(
    user_text: str, session_key: str = "", parent_channel: str = "",
) -> str:
    """Prepend the request's conversational context to a Claude task, if any.

    Scoped to `session_key` (the request's thread) plus the recent user/aria
    exchange from the same `parent_channel` — the room's own timeline. Thread
    internals never bleed between requests, but a brand-new thread is no
    longer amnesiac: "don't do anything with that" can resolve "that" from
    the message sent in the same room seconds earlier (forensic 2026-06-12).
    `exclude_last=1` drops the user turn the caller already recorded so it
    isn't repeated in the task body.
    """
    ctx = conversation.as_claude_context(
        max_turns=10, exclude_last=1, session_key=session_key,
        parent_channel=parent_channel,
    )
    if not ctx:
        return user_text
    return f"{ctx}\nUser just said: {user_text}"


async def _handle_text_conversation(message: discord.Message) -> None:
    """Route a conversational message through Claude in its own thread.

    Every top-level message opens a dedicated Discord thread; the opener
    stays in the channel as a clean index entry and the work — ack, live
    steps, result — happens in the thread. A message typed inside an
    existing thread continues that thread's isolated session. `session_key`
    is the thread id, so concurrent requests never collide and never bleed
    context into each other.

    The user's message and Aria's reply are recorded (tagged with the
    thread's session_key) so this thread's next turn has continuity and a
    live Gemini voice session stays in sync — but the reply comes back as
    text. The user gets the medium they asked for.
    """
    user_text = message.content.strip()
    if not user_text:
        return

    if message.attachments:
        attach_names = [a.filename for a in message.attachments]
        user_text = (
            f"{user_text}\n\n"
            f"[User attached {len(attach_names)} file(s): {', '.join(attach_names)}. "
            f"Vision-on-text routing is not yet wired — describe what you'd do with the "
            f"attachment and ask the user to share its contents inline if needed.]"
        )

    # The isolation primitive: resolve (or open) this request's own thread.
    # A create failure is surfaced loudly — never silently downgraded to an
    # in-channel reply.
    try:
        target, session_key = await _ensure_work_thread(
            message.channel, user_text, anchor=message
        )
    except discord.Forbidden:
        log.exception("Cannot open a thread in %s — missing permission", message.channel)
        await message.channel.send(
            "I couldn't open a thread here — I need the **Create Public Threads** "
            "permission in this channel. Grant it and resend."
        )
        return

    # The room this request lives in: a thread's parent channel, or the
    # channel itself for a top-level message. This is the continuity scope a
    # sibling request in the same room may inherit (user/aria turns only).
    parent_channel = str(
        getattr(target, "parent_id", None) or getattr(message.channel, "id", "")
    )
    channel_name = f"#{getattr(target, 'name', getattr(target, 'id', ''))}"
    conversation.add_user_text(
        channel=channel_name, text=user_text, session_key=session_key,
        parent_channel=parent_channel,
    )

    if gemini and gemini.connected:
        try:
            await gemini.inject_text(
                f"[Heads-up: user just sent text in thread {channel_name}: {user_text[:4000]}]",
                turn_complete=False,
            )
        except Exception:
            log.exception("Failed to inject user text into Gemini live context")

    # Loud one-line ack in the thread so a long do_with_claude loop (the
    # 42c.pw failure ran for 36 min) never looks silent. The body quotes the
    # first ~120 chars so the user can verify we read the request correctly.
    try:
        ack_preview = user_text.strip().replace("\n", " ")
        if len(ack_preview) > 120:
            ack_preview = ack_preview[:120] + "…"
        await target.send(
            f"Got it \u2014 working on: \u201c{ack_preview}\u201d. "
            f"I'll show each step here as I go, then the result.",
            silent=True,
        )
    except Exception:
        log.debug("Failed to send text-conversation ack (non-fatal)", exc_info=True)

    from .tools import handle_tool_call
    async with target.typing():
        try:
            result = await handle_tool_call("do_with_claude", {
                "task": _augment_with_context(user_text, session_key, parent_channel),
                "session_key": session_key,
            })
        except Exception:
            log.exception("Text conversation route failed")
            await target.send(
                "Something went wrong handling that — check #ucs-alerts for details."
            )
            return

    # P4: never leak control-plane errors to the user. Log raw, send friendly.
    if _looks_like_control_plane_error(result):
        log.error("control-plane error quarantined (text-conv): %s", result[:500])
        await target.send(_CONTROL_PLANE_FRIENDLY)
        return
    await _send_chunked(target, result)
    conversation.add_aria_text(
        channel=channel_name, text=result, session_key=session_key,
        parent_channel=parent_channel,
    )

    if gemini and gemini.connected:
        try:
            await gemini.inject_text(
                f"[You just replied via text in thread {channel_name}: {result[:4000]}]",
                turn_complete=False,
            )
        except Exception:
            log.exception("Failed to inject Aria reply into Gemini live context")


def _is_stream_or_capability_channel(channel) -> bool:
    """True for channels Aria must NOT treat as conversation.

    Excludes the silent #ucs-alerts STREAM (responding there would loop on her
    own firehose) and capability-owned channels (e.g. SpicyLit's Grok voice
    channel, which owns its own pipeline). Everything else — every text channel,
    voice text chat, thread, and DM — is fair game so Corbin can talk to Aria
    anywhere and actually get an answer back.
    """
    try:
        cid = str(getattr(channel, "id", ""))
    except Exception:
        return False
    excluded = {
        str(config.discord_log_channel_id or ""),
        str(config.discord_spicylit_channel_id or ""),
    }
    excluded.discard("")
    if cid in excluded:
        return True
    # A thread inherits its parent's exclusion: a thread hanging off the silent
    # #ucs-alerts stream is still part of that stream, so Aria must not treat
    # it as conversation (which would loop on her own firehose).
    if isinstance(channel, discord.Thread):
        return str(getattr(channel, "parent_id", "") or "") in excluded
    return False


@bot.event
async def on_message(message):
    """Route inbound Discord messages.

    Priority order:
      1. Bot/self messages: ignore (avoid loops).
      2. Webhook !ask: route to _run_ask (existing iOS-Shortcut path).
      3. Command prefix !: delegate to bot.process_commands.
      4. Plain text from the authorized user in ANY channel (or DM): the
         conversational path through Claude. Corbin types to Aria all over;
         she answers wherever he is. Only the silent alerts stream and
         capability channels are excluded.
      5. Other authors / excluded channels: ignored.
    """
    if message.author.id == bot.user.id:
        return
    if message.author.bot and not message.webhook_id:
        return

    if message.webhook_id and message.content.startswith("!ask "):
        task_text = message.content[5:].strip()
        if task_text:
            await _run_ask(message.channel, task_text)
        return

    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    if (
        _is_authorized(message.author.id)
        and message.content.strip()
        and not _is_stream_or_capability_channel(message.channel)
    ):
        await _handle_text_conversation(message)
        return


# ---------------------------------------------------------------------------
# SpicyLit: Grok Voice Agent
# ---------------------------------------------------------------------------

async def _drain_grok_to_voice() -> None:
    """Drain Grok's 24kHz mono audio output to the voice sidecar."""
    try:
        while _grok_session and _grok_session.connected:
            pcm = await _grok_session.get_audio()
            if pcm is None:
                log.info("Grok audio stream ended (session closed)")
                return
            await voice_bridge.send_audio(pcm)
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("Grok -> voice drain task error")


async def _on_grok_disconnect(reason: str) -> None:
    """Called by GrokVoiceSession when the WebSocket drops unexpectedly."""
    global _spicylit_active, _grok_session, _grok_session_started_at
    log.error("Grok session disconnected: %s", reason)
    _log_grok_cost()
    _spicylit_active = False
    _grok_session = None
    _cancel_audio_tasks()
    try:
        await post_to_alerts(f"**SpicyLit disconnected:** {reason}")
    except Exception:
        log.exception("Failed to post Grok disconnect alert")


def _log_grok_cost() -> None:
    """Log estimated Grok voice session cost to the events table."""
    global _grok_session_started_at
    if _grok_session_started_at <= 0:
        return
    elapsed_min = (time.monotonic() - _grok_session_started_at) / 60.0
    cost = elapsed_min * GROK_COST_PER_MINUTE
    log_event(
        tool_name="grok_voice_session",
        result=f"{elapsed_min:.1f} min",
        duration_ms=int(elapsed_min * 60_000),
        token_cost_usd=cost,
    )
    log.info("Grok session cost: $%.3f (%.1f min)", cost, elapsed_min)
    _grok_session_started_at = 0.0


async def _grok_watchdog_task() -> None:
    """Pause Grok session after GROK_IDLE_TIMEOUT_SEC of silence.

    Logs cost for the active segment. Session resumes transparently
    when the user speaks again (handled by _on_voice_audio).
    """
    global _last_audio_received_at, _grok_paused
    try:
        while _grok_session and _grok_session.connected:
            await asyncio.sleep(5)
            if _last_audio_received_at == 0:
                continue
            idle = time.monotonic() - _last_audio_received_at
            if idle > GROK_IDLE_TIMEOUT_SEC and _grok_session and _grok_session.connected:
                log.info("%ds silence — pausing Grok session", GROK_IDLE_TIMEOUT_SEC)
                _log_grok_cost()
                try:
                    await _grok_session.close()
                except Exception:
                    log.exception("Error pausing Grok session")
                _grok_paused = True
                _last_audio_received_at = 0.0
                log.info("Grok paused. Will auto-resume when user speaks.")
                return
    except asyncio.CancelledError:
        pass


async def _post_to_spicylit(content: str) -> None:
    """Post to #spicy-lit text channel. Fails loudly if misconfigured."""
    ch_id = config.discord_spicylit_channel_id
    if not ch_id:
        raise RuntimeError(
            "DISCORD_SPICYLIT_CHANNEL_ID not set — cannot post SpicyLit outline"
        )
    ch = bot.get_channel(int(ch_id))
    if not ch:
        raise RuntimeError(
            f"SpicyLit channel {ch_id} not found — check DISCORD_SPICYLIT_CHANNEL_ID"
        )
    for chunk in _split_at_paragraphs(content):
        await ch.send(chunk)


@bot.command()
async def spicylit(ctx: commands.Context, mode: str = "story"):
    """Switch voice to Grok SpicyLit mode.  Usage: !spicylit [story|joi]

    Serialized under VoiceController.pipeline_switch() — same lock as
    !join and _auto_join_voice_channel, so a manual !spicylit cannot
    race with the channel-based auto-route.
    """
    global _spicylit_active, _grok_session, _grok_session_started_at

    if _spicylit_active and _grok_session and _grok_session.connected:
        await ctx.send("SpicyLit already active. Use !back to return to Aria.")
        return

    if not _is_authorized(ctx.author.id):
        await ctx.send("Not authorized.")
        return
    if not voice_bridge.alive:
        await ctx.send("Voice bridge not running — join voice first with !join.")
        return
    if not config.grok_api_key:
        await ctx.send("GROK_API_KEY not set.")
        return

    from capabilities.spicy_lit import GrokVoiceSession, init_table
    from capabilities.spicy_lit.prompts import VALID_MODES

    mode = mode.lower().strip()
    if mode not in VALID_MODES:
        await ctx.send(f"Unknown mode `{mode}`. Valid modes: {', '.join(sorted(VALID_MODES))}")
        return

    try:
        async with voice_controller.pipeline_switch():
            if _spicylit_active and _grok_session and _grok_session.connected:
                await ctx.send("SpicyLit already active (resolved while waiting on lock).")
                return

            init_table()

            if gemini and gemini.connected:
                await gemini.close()
            _cancel_audio_tasks()

            _grok_session = GrokVoiceSession(
                api_key=config.grok_api_key,
                voice="eve",
                user_id=str(ctx.author.id),
                mode=mode,
                post_text_callback=_post_to_spicylit,
                on_disconnect=_on_grok_disconnect,
            )
            await _grok_session.start()
            _spicylit_active = True
            _grok_session_started_at = time.monotonic()

            _audio_tasks.append(asyncio.create_task(_drain_grok_to_voice()))
            _audio_tasks.append(asyncio.create_task(_grok_watchdog_task()))
            await ctx.send(f"SpicyLit **{mode}** mode active. Grok is listening.")
    except VoiceTransitionBusy:
        await ctx.send("A voice transition is already in progress — try again in a moment.")


@bot.command()
async def back(ctx: commands.Context):
    """Switch voice back to Gemini from SpicyLit mode.

    Serialized under VoiceController.pipeline_switch() so it cannot
    race with !spicylit or the auto-join path.
    """
    global _spicylit_active, _grok_session

    if not _is_authorized(ctx.author.id):
        await ctx.send("Not authorized.")
        return
    if not _spicylit_active:
        await ctx.send("Not in SpicyLit mode.")
        return

    try:
        async with voice_controller.pipeline_switch():
            if not _spicylit_active:
                await ctx.send("SpicyLit already disabled (resolved while waiting on lock).")
                return

            _cancel_audio_tasks()
            _log_grok_cost()
            if _grok_session:
                await _grok_session.close()
                _grok_session = None
            _spicylit_active = False

            if gemini and not gemini.connected:
                await gemini.connect()
            _audio_tasks.append(asyncio.create_task(_drain_gemini_to_voice(gemini)))
            _audio_tasks.append(asyncio.create_task(_watchdog_task(gemini)))

            await ctx.send("Back to Aria.")
    except VoiceTransitionBusy:
        await ctx.send("A voice transition is already in progress — try again in a moment.")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    """Auto-join when the authorized user enters voice; clean up when they leave."""
    global _spicylit_active, _grok_session
    global _session_paused, _grok_paused, _pause_transcript
    global _pending_voice_channel_id
    if str(member.id) not in config.authorized_user_ids:
        return

    joined_channel = after.channel and (not before.channel or before.channel.id != after.channel.id)
    left_channel = before.channel and not after.channel

    if joined_channel and after.channel:
        if not _preflight_passed:
            _pending_voice_channel_id = str(after.channel.id)
            log.info(
                "User joined %s during preflight — deferring auto-join until preflight completes",
                after.channel.name,
            )
            return
        if not voice_bridge.alive:
            log.warning("User joined voice but voice bridge not alive — skipping auto-join")
            return

        log.info("Authorized user joined %s — auto-joining", after.channel.name)
        await _auto_join_voice_channel(after.channel)

    elif left_channel:
        lurk = bool(config.aria_lurk_in_voice)
        log.info(
            "Authorized user left voice — cleaning up%s",
            " (lurk mode: bot stays in channel)" if lurk else "",
        )
        _session_paused = False
        _grok_paused = False
        _pause_transcript = ""
        _cancel_audio_tasks()
        if _spicylit_active and _grok_session:
            if _grok_session.connected:
                await _grok_session.close()
            _grok_session = None
            _spicylit_active = False
        if gemini and gemini.connected:
            await gemini.close()
        if lurk:
            # Keep the voice WebSocket parked in this channel. The next join
            # reconciles through _auto_join_voice_channel / ensure_in_channel:
            # same channel re-arms in place, a different channel moves the
            # sidecar. Lurk is purely this leave-time "don't depart" policy.
            await voice_controller.note_external_disconnect_lurk()
        else:
            await voice_controller.note_external_disconnect()


def _is_authorized(user_id: int) -> bool:
    if not config.authorized_user_ids:
        return True
    return str(user_id) in config.authorized_user_ids


def main():
    if not config.discord_bot_token:
        log.error("DISCORD_APP_BOT_TOKEN not set.")
        sys.exit(1)
    bot.run(config.discord_bot_token)


if __name__ == "__main__":
    main()
