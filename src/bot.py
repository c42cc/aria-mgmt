"""Main entry point. The whole loop."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import time

import discord
from discord.ext import commands

from .config import config
from .conversation import conversation
from .cursor_bridge import CursorBridge
from .db import init_db, get_daily_spend, log_event, upsert_cursor_session, update_cursor_session_event
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

_wake_listener = None          # WakeWordListener | None (typed loosely to avoid import at module level)
_local_speaker: SpeakerOutput | None = None
_local_session_active: bool = False
_local_silence_task: asyncio.Task | None = None

_on_ready_done = False

IDLE_TIMEOUT_SEC = 25
GROK_IDLE_TIMEOUT_SEC = 25
GROK_COST_PER_MINUTE = 0.05
VOICE_EXIT_TIMEOUT_SEC = 600  # leave voice after 10 min of total silence
LOCAL_SILENCE_TIMEOUT_SEC = 8  # close local voice session after 8s idle


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

def _split_at_paragraphs(text: str, max_len: int = 1900) -> list[str]:
    """Split text into chunks at paragraph boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        if len(current) + len(para) + 2 > max_len:
            if current:
                chunks.append(current.strip())
            current = para
        else:
            current = current + "\n\n" + para if current else para
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
    target = thread or ch
    if len(content) > 6000:
        await target.send(
            "Full output attached:",
            file=discord.File(io.BytesIO(content.encode()), filename="output.md"),
        )
    else:
        for chunk in _split_at_paragraphs(content):
            await target.send(chunk)


async def post_to_alerts(content: str) -> None:
    """Post to #ucs-alerts. Recorded in the conversation buffer as a
    system alert so text-Aria can answer 'what was that error?' from
    `#ucs`."""
    ch = bot.get_channel(int(config.discord_log_channel_id))
    if not ch:
        raise RuntimeError(f"Alert channel {config.discord_log_channel_id} not found — bot cannot post alerts")
    for chunk in _split_at_paragraphs(content):
        await ch.send(chunk)
    conversation.add_alert(content)


async def _mirror_to_voice_chat(prefix: str, text: str) -> None:
    """Post text to the currently-active voice channel's text-in-voice chat.

    No-op when the bot is not in a voice channel. Used to mirror Aria's
    spoken replies (and the user's transcribed utterances) so the voice
    session has a scrollable transcript visible in the same Discord
    channel as the audio.
    """
    if not voice_controller.channel_id:
        return
    ch = bot.get_channel(int(voice_controller.channel_id))
    if not ch or not isinstance(ch, discord.VoiceChannel):
        return
    body = f"**{prefix}** {text}" if prefix else text
    for chunk in _split_at_paragraphs(body):
        try:
            await ch.send(chunk)
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
        if gemini and gemini.connected:
            await gemini.send_audio(pcm)


async def _drain_gemini_to_voice(session: GeminiSession) -> None:
    """Drain Gemini's 24kHz mono audio output and stream it to the voice sidecar."""
    try:
        while session.connected:
            pcm = await session.get_audio()
            await voice_bridge.send_audio(pcm)
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("Gemini -> voice drain task error")


async def _watchdog_task(session: GeminiSession) -> None:
    """Pause Gemini session after IDLE_TIMEOUT_SEC of silence.

    Saves transcript context so the session resumes seamlessly
    when the user speaks again (handled by _on_voice_audio).
    """
    global _last_audio_received_at, _session_paused, _pause_transcript
    try:
        while True:
            await asyncio.sleep(5)
            if _last_audio_received_at == 0:
                continue
            idle = time.monotonic() - _last_audio_received_at
            if idle > IDLE_TIMEOUT_SEC and session.connected:
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


async def _auto_join_voice_channel(channel: discord.VoiceChannel) -> None:
    """Join the voice channel and bring up the right AI session for it.

    Used by both on_voice_state_update (when user joins voice) and the
    on_ready post-preflight rescan (when the user was already in voice
    or joined during preflight).

    Picks the Grok/SpicyLit pipeline when the channel matches
    DISCORD_SPICYLIT_CHANNEL_ID, otherwise the Gemini/Aria pipeline.
    """
    global _spicylit_active, _grok_session, _grok_session_started_at
    global _last_audio_received_at

    is_spicylit = (
        config.discord_spicylit_channel_id
        and str(channel.id) == config.discord_spicylit_channel_id
    )

    if voice_controller.in_voice:
        if is_spicylit and _spicylit_active and _grok_session and _grok_session.connected:
            log.info("SpicyLit already active in %s — skipping duplicate join", channel.name)
            return
        if not is_spicylit and gemini and gemini.connected:
            log.info("Gemini already active in voice — skipping duplicate join")
            return

    if not voice_bridge.alive:
        log.warning("Asked to auto-join %s but voice bridge not alive", channel.name)
        return

    if _local_session_active:
        await _close_local_session()
    if _wake_listener:
        _wake_listener.pause()

    joined = await voice_controller.join(
        str(channel.id),
        audio_callback=_on_voice_audio,
        on_watchdog_expire=_on_voice_exit_timeout,
    )
    if not joined:
        log.info("Voice join skipped for %s (already in voice or transition in progress)", channel.name)
        return

    log.info("Joined voice channel %s", channel.name)
    _last_audio_received_at = 0.0
    _cancel_audio_tasks()

    if is_spicylit:
        if not config.grok_api_key:
            log.error("Joined #spicy-lit but GROK_API_KEY not set — no audio pipeline")
            return

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
                return

        _audio_tasks.append(asyncio.create_task(_drain_gemini_to_voice(gemini)))
        _audio_tasks.append(asyncio.create_task(_watchdog_task(gemini)))

        if gemini and gemini.connected:
            try:
                buffer_ctx = conversation.as_gemini_injection(max_turns=10)
                preamble = "[Context: Corbin just joined the voice channel. Stay silent until he speaks.]"
                injection = f"{preamble}\n\n{buffer_ctx}" if buffer_ctx else preamble
                await gemini.inject_text(injection, turn_complete=False)
            except Exception:
                log.exception("Failed to inject join context into Gemini")


def _find_authorized_user_voice_channel() -> discord.VoiceChannel | None:
    """Return the voice channel the authorized user is currently in, if any."""
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for m in vc.members:
                if str(m.id) in config.authorized_user_ids:
                    return vc
    return None


# ---------------------------------------------------------------------------
# Cursor event consumer
# ---------------------------------------------------------------------------

async def _cursor_event_consumer(
    session_id: str, thread: discord.Thread | None
) -> None:
    """Read Cursor build events and route to Discord + Gemini."""
    last_post = 0.0
    try:
        async for event in cursor_bridge.read_events(session_id):
            if _cancel_flag:
                break
            etype = event.get("event", "")

            if etype in ("file_edit", "test_run"):
                now = time.monotonic()
                if now - last_post > 5 and thread:
                    summary = event.get("data", {}).get("summary", etype)
                    await thread.send(f"`{etype}`: {str(summary)[:500]}")
                    last_post = now
                update_cursor_session_event(session_id, etype)

            elif etype == "question" and gemini:
                question = event.get("data", {}).get("text", "Cursor has a question")
                await gemini.inject_text(
                    f"Cursor is asking: {question}", turn_complete=True
                )

            elif etype == "completion":
                if thread:
                    await thread.send("Build complete.")
                if gemini and gemini.connected:
                    await gemini.inject_text(
                        "The Cursor build has completed successfully.", turn_complete=True
                    )
                upsert_cursor_session(session_id, "", status="completed")
                break

            elif etype == "error":
                msg = event.get("data", {}).get("message", "Unknown error")
                await post_to_alerts(f"Cursor error ({session_id[:8]}): {msg}")
                upsert_cursor_session(session_id, "", status="error")
                break
    except Exception:
        log.exception("Cursor event consumer error for %s", session_id)
    finally:
        cursor_bridge.close_session(session_id)


# ---------------------------------------------------------------------------
# Bot events and commands
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    global gemini, _preflight_passed, _last_preflight_report
    global _pending_voice_channel_id
    global _on_ready_done
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
                await post_to_alerts(f"**Confirmation required:**\n`{tool_name}`: {summary}")
                if gemini and gemini.connected:
                    await gemini.inject_text(
                        f"I need your approval. About to run: {tool_name}. Details: {summary}. "
                        "Do you approve? Say yes or no.",
                        turn_complete=True,
                    )
                return await gemini.wait_for_confirmation(action_id, timeout=60.0)

            mcp.set_confirm_callback(_confirm_callback)
        except Exception:
            log.exception("MCP fleet failed to start — preflight will flag MCP probes")

        from .preflight import run_all, format_report
        report = await run_all(
            mcp_client=mcp,
            cursor_bridge=cursor_bridge,
            alert_callback=post_to_alerts,
            include_gemini=True,
            include_cursor=True,
        )
        _last_preflight_report = report
        _preflight_passed = report.ok

        formatted = format_report(report, markdown=True)
        try:
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
    """Join the voice channel and start Gemini session.

    Idempotent: if we are already in voice with a healthy Gemini session,
    short-circuits with a one-liner. The VoiceController serializes all
    voice transitions so concurrent !join / auto-join cannot race.
    """
    global _last_audio_received_at

    if voice_controller.in_voice and gemini and gemini.connected:
        await ctx.send("Already in voice.")
        return

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

    if voice_controller.locked:
        await ctx.send("A voice join is already in progress — skipping duplicate.")
        return

    if _local_session_active:
        await _close_local_session()
    if _wake_listener:
        _wake_listener.pause()

    joined = await voice_controller.join(
        str(ctx.author.voice.channel.id),
        audio_callback=_on_voice_audio,
        on_watchdog_expire=_on_voice_exit_timeout,
    )
    if not joined:
        if voice_controller.in_voice:
            await ctx.send("Already in voice (resolved by auto-join while you typed `!join`).")
        else:
            await ctx.send("A voice transition completed — try again.")
        return

    _last_audio_received_at = 0.0
    _cancel_audio_tasks()

    await ctx.send(f"Joined {ctx.author.voice.channel.name}")

    if gemini and not gemini.connected:
        await gemini.connect()

    if gemini and gemini.connected:
        try:
            buffer_ctx = conversation.as_gemini_injection(max_turns=10)
            if buffer_ctx:
                await gemini.inject_text(buffer_ctx, turn_complete=False)
        except Exception:
            log.exception("Failed to inject conversation buffer on !join")

    _audio_tasks.append(asyncio.create_task(_drain_gemini_to_voice(gemini)))
    _audio_tasks.append(asyncio.create_task(_watchdog_task(gemini)))


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


async def _run_ask(channel, message: str) -> None:
    """Shared implementation for !ask — works for both commands and webhook messages.

    This is the *programmatic* entry point (iOS Shortcuts, webhook tests,
    automated callers). Every invocation is a clean slate: we do **not**
    prepend conversation buffer context here. Two reasons:

    1. Test reproducibility — the same `!ask` should produce the same
       agent loop regardless of what previous text exchanges polluted
       the buffer.
    2. Programmatic callers do not have a 'conversation' — they have a
       request. Borrowing context from a prior session can short-circuit
       tool calls the new request actually requires (verified: the agent
       once skipped `mail_messages` because the prior reply was in the
       task body).

    The natural-text-in-`#ucs` path (`_handle_text_conversation`) is
    where conversational continuity belongs.
    """
    channel_name = f"#{getattr(channel, 'name', channel.id)}"
    conversation.add_user_text(channel=channel_name, text=message)
    await channel.send("Working on it...")
    from .tools import handle_tool_call
    session_key = str(channel.id)
    try:
        result = await handle_tool_call("do_with_claude", {
            "task": message,
            "session_key": session_key,
        })
        if len(result) > 1900:
            await post_to_text(result)
            await channel.send("Done — full result posted to #ucs.")
        else:
            await channel.send(result)
        conversation.add_aria_text(channel=channel_name, text=result)
    except Exception as e:
        await channel.send(f"Error: {e}")


def _augment_with_context(user_text: str) -> str:
    """Prepend recent conversation thread to a Claude task, if any.

    `exclude_last=1` drops the user turn the caller already recorded so
    it isn't repeated in the task body.
    """
    ctx = conversation.as_claude_context(max_turns=10, exclude_last=1)
    if not ctx:
        return user_text
    return f"{ctx}\nUser just said: {user_text}"


async def _handle_text_conversation(message: discord.Message) -> None:
    """Route a conversational message in #ucs through Claude.

    Same dispatch as !ask but without requiring the prefix, and the
    user's message + Aria's reply are recorded in the conversation
    buffer so the next voice session has full context.

    If a Gemini session is currently connected (user is on voice while
    also typing), we forward the exchange into Gemini's live context so
    voice-Aria stays in sync turn-by-turn — but the *reply* still comes
    back as text. The user gets the medium they asked for.
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

    channel_name = f"#{getattr(message.channel, 'name', message.channel.id)}"
    conversation.add_user_text(channel=channel_name, text=user_text)

    if gemini and gemini.connected:
        try:
            await gemini.inject_text(
                f"[Heads-up: user just sent text in {channel_name}: {user_text[:4000]}]",
                turn_complete=False,
            )
        except Exception:
            log.exception("Failed to inject user text into Gemini live context")

    from .tools import handle_tool_call
    async with message.channel.typing():
        try:
            result = await handle_tool_call("do_with_claude", {
                "task": _augment_with_context(user_text),
                "session_key": str(message.channel.id),
            })
        except Exception:
            log.exception("Text conversation route failed")
            await message.channel.send(
                "Something went wrong handling that — check #ucs-alerts for details."
            )
            return

    if len(result) <= 1900:
        await message.channel.send(result)
    else:
        await post_to_text(result)
        await message.channel.send("Done — full result posted to #ucs.")
    conversation.add_aria_text(channel=channel_name, text=result)

    if gemini and gemini.connected:
        try:
            await gemini.inject_text(
                f"[You just replied via text in {channel_name}: {result[:4000]}]",
                turn_complete=False,
            )
        except Exception:
            log.exception("Failed to inject Aria reply into Gemini live context")


@bot.event
async def on_message(message):
    """Route inbound Discord messages.

    Priority order:
      1. Bot/self messages: ignore (avoid loops).
      2. Webhook !ask: route to _run_ask (existing iOS-Shortcut path).
      3. Command prefix !: delegate to bot.process_commands.
      4. Plain text from authorized user in #ucs: conversational path
         through Claude.
      5. Anything else: ignored, but #ucs-alerts content is already
         recorded into the conversation buffer by post_to_alerts when
         Aria/the bot posts it.
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
        config.discord_text_channel_id
        and message.channel.id == int(config.discord_text_channel_id)
        and _is_authorized(message.author.id)
        and message.content.strip()
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
        log.info("Authorized user left voice — cleaning up")
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
