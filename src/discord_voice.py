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
import json
import logging
import os
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
