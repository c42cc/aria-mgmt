"""Voice transport. Owns a Node.js sidecar that owns the Discord voice WebSocket.

Why a sidecar: py-cord (and discord.py) do not implement Discord's mandatory
DAVE E2EE protocol (close code 4017 on every voice connect since March 2026).
discord.js does. The sidecar is a second Discord application logged in as a
voice-only bot; the existing py-cord bot (Bot #1) keeps doing text, commands,
threads, events — unchanged. The two never talk through Discord. They are glued
by this Python process.

Audio direction conventions:
  Discord -> Python:  16 kHz mono PCM s16le  (Node downsamples from 48k stereo)
  Python -> Discord:  24 kHz mono PCM s16le  (Node upsamples to 48k stereo)

Both formats are exactly what Gemini Live expects/produces, so this module does
no audio processing of its own.
"""

from __future__ import annotations

import asyncio
import base64
import enum
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable

from .config import config

log = logging.getLogger(__name__)

AudioCallback = Callable[[bytes], Awaitable[None]]

_BRIDGE_DIR = Path(__file__).resolve().parent.parent / "discord_voice_bridge"
_READY_TIMEOUT_SEC = 15.0


class VoiceBridge:
    """Spawn and supervise discord_voice_bridge/index.js.

    Public API is intentionally small. All write methods are fire-and-forget;
    lifecycle events (`ready`, `joined`, `left`, `error`) are logged.
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._on_audio: AudioCallback | None = None
        self._ready = asyncio.Event()
        self._joined = asyncio.Event()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        """Spawn the Node sidecar; return once Discord login completes."""
        if self.alive:
            return
        if not config.discord_voice_bot_token:
            raise RuntimeError(
                "DISCORD_VOICE_BOT_TOKEN not set — create a second Discord "
                "application for voice and put its token in .env."
            )

        env = os.environ.copy()
        env["DISCORD_VOICE_BOT_TOKEN"] = config.discord_voice_bot_token
        env["AUTHORIZED_VOICE_USER_ID"] = config.authorized_voice_user_id

        self._proc = await asyncio.create_subprocess_exec(
            "node", "index.js",
            cwd=str(_BRIDGE_DIR),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        log.info("voice bridge subprocess started (pid=%s)", self._proc.pid)

        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_READY_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"voice bridge did not log in to Discord within {_READY_TIMEOUT_SEC}s"
            )
        log.info("voice bridge ready")

    def register_audio_callback(self, fn: AudioCallback) -> None:
        """Frames from Discord arrive here as 16 kHz mono PCM bytes."""
        self._on_audio = fn

    async def join(self, channel_id: str, timeout: float = 70.0) -> None:
        """Send join action; wait for sidecar to confirm or time out.

        Default timeout is 70s because the sidecar internally retries voice
        connect up to 3 times at 20s each plus backoffs (~64s worst case).
        """
        self._joined.clear()
        await self._send({"action": "join", "channel_id": str(channel_id)})
        try:
            await asyncio.wait_for(self._joined.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.error("voice bridge join timed out after %.0fs", timeout)

    async def leave(self) -> None:
        await self._send({"action": "leave"})

    async def send_audio(self, pcm: bytes) -> None:
        """Send 24 kHz mono PCM to Discord (Node upsamples to 48 kHz stereo)."""
        if not self.alive or not pcm:
            return
        await self._send({"action": "play", "pcm_b64": base64.b64encode(pcm).decode()})

    async def close(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                await self._send({"action": "shutdown"})
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.terminate()
                await self._proc.wait()
        for t in (self._reader_task, self._stderr_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    async def _send(self, obj: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("voice bridge not started")
        async with self._write_lock:
            self._proc.stdin.write((json.dumps(obj) + "\n").encode())
            await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    log.error("voice bridge stdout closed; sidecar likely exited")
                    return
                try:
                    evt = json.loads(raw.decode())
                except json.JSONDecodeError:
                    log.warning("voice bridge non-JSON line: %r", raw[:200])
                    continue
                await self._handle_event(evt)
        except asyncio.CancelledError:
            pass

    async def _handle_event(self, evt: dict) -> None:
        name = evt.get("event")
        if name == "ready":
            self._ready.set()
        elif name == "joined":
            log.info("voice bridge joined channel %s", evt.get("channel_id"))
            self._joined.set()
        elif name == "left":
            log.info("voice bridge left voice")
        elif name == "audio":
            if self._on_audio is None:
                return
            try:
                pcm = base64.b64decode(evt["pcm_b64"])
            except Exception:
                log.exception("voice bridge bad audio b64")
                return
            try:
                await self._on_audio(pcm)
            except Exception:
                log.exception("voice audio callback raised")
        elif name == "error":
            log.error("voice bridge error: %s", evt.get("message"))
        else:
            log.warning("voice bridge unknown event: %s", evt)

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                log.warning("voice bridge stderr: %s", line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            pass


voice_bridge = VoiceBridge()


# ---------------------------------------------------------------------------
# Voice lifecycle state machine
# ---------------------------------------------------------------------------

class VoiceState(enum.Enum):
    """Typed voice-lifecycle states. The controller is always in exactly one."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    IN_VOICE = "in_voice"
    DISCONNECTING = "disconnecting"


class VoiceTransitionBusy(Exception):
    """Raised when a voice transition is requested while another is in flight."""


class VoiceController:
    """Single owner of Discord-voice lifecycle. Process-singleton.

    Same shape as GeminiSession / CursorBridge / MCPClient: owns its lock,
    its state, and its watchdog task. Public methods are the only valid
    transitions; the flag/lock/task triplet is encapsulated.

    The lock is global (not per-channel) because Discord allows only one
    voice connection per bot per guild. This is the correct asymmetry with
    the per-session_key agent locks in tools.py: agent loops are parallel
    across channels; voice is exclusive at the Discord layer.
    """

    def __init__(
        self,
        bridge: VoiceBridge,
        *,
        watchdog_timeout_sec: float = 600,
    ) -> None:
        self._bridge = bridge
        self._state = VoiceState.DISCONNECTED
        self._lock = asyncio.Lock()
        self._watchdog_task: asyncio.Task | None = None
        self._channel_id: str | None = None
        self._last_activity_at: float = 0.0
        self._watchdog_timeout_sec = watchdog_timeout_sec
        self._on_watchdog_expire: Callable[[], Awaitable[None]] | None = None

    @property
    def in_voice(self) -> bool:
        return self._state is VoiceState.IN_VOICE

    @property
    def state(self) -> VoiceState:
        return self._state

    @property
    def channel_id(self) -> str | None:
        return self._channel_id

    @property
    def locked(self) -> bool:
        return self._lock.locked()

    def touch(self) -> None:
        """Record user activity. Resets the exit-watchdog idle timer."""
        self._last_activity_at = time.monotonic()

    async def join(
        self,
        channel_id: str,
        *,
        audio_callback: AudioCallback,
        on_watchdog_expire: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """DISCONNECTED -> IN_VOICE. Idempotent, serialized.

        Returns True if the join was performed, False if already in the
        requested state or a transition is in flight.
        """
        if self._lock.locked():
            return False
        async with self._lock:
            if self._state is VoiceState.IN_VOICE and self._channel_id == channel_id:
                return False
            self._state = VoiceState.CONNECTING
            try:
                self._bridge.register_audio_callback(audio_callback)
                await self._bridge.join(channel_id)
                self._channel_id = channel_id
                self._state = VoiceState.IN_VOICE
                self._on_watchdog_expire = on_watchdog_expire
                self._last_activity_at = time.monotonic()
                if not config.aria_lurk_in_voice:
                    # In lurk mode the watchdog would eventually trip and
                    # call bridge.leave() on user silence, undoing the
                    # whole point. The bot relies on voice_state_update
                    # for cleanup instead.
                    self._spawn_watchdog()
                return True
            except Exception:
                self._state = VoiceState.DISCONNECTED
                raise

    async def leave(self) -> bool:
        """IN_VOICE -> DISCONNECTED. Calls bridge.leave(). Idempotent."""
        if self._state is VoiceState.DISCONNECTED:
            return False
        async with self._lock:
            return await self._do_leave()

    async def note_external_disconnect(self) -> None:
        """Handle the user leaving voice. The bot leaves the channel too.

        Waits for any in-progress transition to complete, then disconnects.
        Unlike leave(), this is always called from on_voice_state_update
        where the user has already departed — the bridge.leave() tells the
        sidecar to depart as well.
        """
        async with self._lock:
            self._cancel_watchdog()
            if self._state is not VoiceState.DISCONNECTED:
                try:
                    await self._bridge.leave()
                except Exception:
                    log.exception("Error leaving voice channel on external disconnect")
            self._state = VoiceState.DISCONNECTED
            self._channel_id = None
            self._last_activity_at = 0.0

    async def note_external_disconnect_lurk(self) -> None:
        """User left, but the bot stays in voice (lurk mode).

        Cancels the silence watchdog so it does not eventually trigger
        bridge.leave() on its own, and zeroes activity so the next user
        rejoin starts a fresh idle window. The bridge WebSocket and the
        IN_VOICE state are intentionally preserved — the audio sidecar
        keeps the channel slot, only the per-conversation pipeline
        (Gemini, audio drains) is torn down by the caller.
        """
        async with self._lock:
            self._cancel_watchdog()
            self._last_activity_at = 0.0

    async def rearm_watchdog_for_lurk(
        self,
        *,
        audio_callback: Callable[[bytes], Awaitable[None]],
        on_watchdog_expire: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Lurk-mode rejoin path. The user came back into the channel the
        bot has been quietly sitting in. We do NOT call bridge.join (a
        no-op anyway, since the WebSocket never dropped), but we DO need
        to re-register the audio callback (the bridge keeps it across
        reconnects but defensive re-registration costs nothing).

        Intentionally does NOT respawn the silence watchdog: the whole
        point of lurk mode is to never auto-leave the channel. The
        Gemini session is torn down explicitly on voice_state_update
        when the user departs again, so a silence timeout would just be
        a slow racey duplicate of that path that risks tripping
        bridge.leave() and undoing the lurk.
        """
        async with self._lock:
            if self._state is not VoiceState.IN_VOICE:
                # Shouldn't happen, but degrade gracefully: caller should
                # fall back to the normal join path.
                log.warning(
                    "rearm_watchdog_for_lurk called while not in voice (state=%s) — caller should use join()",
                    self._state,
                )
                return
            self._bridge.register_audio_callback(audio_callback)
            self._on_watchdog_expire = on_watchdog_expire
            self._last_activity_at = time.monotonic()

    @asynccontextmanager
    async def pipeline_switch(self):
        """Context manager for pipeline switches that need the transition
        lock but don't change voice channel state (e.g. !spicylit, !back).

        Raises VoiceTransitionBusy if a transition is already in flight.
        Respawns the exit watchdog on clean context-manager exit.
        """
        if self._lock.locked():
            raise VoiceTransitionBusy()
        async with self._lock:
            yield
            self._spawn_watchdog()

    async def _do_leave(self) -> bool:
        """Internal leave. Caller must hold _lock."""
        if self._state is VoiceState.DISCONNECTED:
            return False
        self._state = VoiceState.DISCONNECTING
        self._cancel_watchdog()
        try:
            await self._bridge.leave()
        except Exception:
            log.exception("Error leaving voice channel")
        self._state = VoiceState.DISCONNECTED
        self._channel_id = None
        self._last_activity_at = 0.0
        return True

    def _spawn_watchdog(self) -> None:
        """Start (or restart) the voice-exit watchdog. Categorically
        prevents stacking: any existing watchdog is cancelled first."""
        self._cancel_watchdog()
        self._watchdog_task = asyncio.create_task(
            self._watchdog(), name="voice_exit_watchdog"
        )

    def _cancel_watchdog(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            if self._watchdog_task is not asyncio.current_task():
                self._watchdog_task.cancel()
        self._watchdog_task = None

    async def _watchdog(self) -> None:
        """Leave voice after sustained user silence."""
        try:
            while True:
                await asyncio.sleep(30)
                if self._state is not VoiceState.IN_VOICE:
                    return
                if self._last_activity_at == 0:
                    continue
                idle = time.monotonic() - self._last_activity_at
                if idle < self._watchdog_timeout_sec:
                    continue
                if self._lock.locked():
                    continue

                async with self._lock:
                    if self._state is not VoiceState.IN_VOICE:
                        return
                    log.info("%.0fs silence — auto-leaving voice channel", idle)
                    if self._on_watchdog_expire:
                        try:
                            await self._on_watchdog_expire()
                        except Exception:
                            log.exception("voice exit watchdog on_expire callback failed")
                    await self._do_leave()
                    return
        except asyncio.CancelledError:
            pass


voice_controller = VoiceController(voice_bridge)
