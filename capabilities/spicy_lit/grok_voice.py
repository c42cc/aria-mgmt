"""Grok Voice Agent API WebSocket session for SpicyLit."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Callable, Coroutine

import websockets

from .pipeline import generate_outline
from .db import save_outline, get_latest_outline
from .prompts import get_session_config, STORY, STORY_POST_OUTLINE_INSTRUCTION, VALID_MODES

log = logging.getLogger(__name__)

GROK_VOICE_URL = "wss://api.x.ai/v1/realtime?model=grok-voice-latest"

# Suppress forwarding user audio while Grok is speaking to prevent the
# echo-feedback loop (Grok response → speakers → microphone → Grok input
# → second response). The cooldown gives Discord's echo cancellation time
# to settle after the last audio frame.
ECHO_SUPPRESSION_COOLDOWN_SEC = 0.8


class GrokVoiceSession:
    """Manages a bidirectional voice session with the Grok Voice Agent API."""

    def __init__(
        self,
        api_key: str,
        voice: str = "eve",
        user_id: str = "",
        mode: str = STORY,
        post_text_callback: Callable[..., Coroutine] | None = None,
        on_disconnect: Callable[[str], Coroutine] | None = None,
    ):
        if mode not in VALID_MODES:
            raise ValueError(
                f"Unknown SpicyLit mode {mode!r}. Valid: {', '.join(sorted(VALID_MODES))}"
            )
        self._api_key = api_key
        self._voice = voice
        self._user_id = user_id
        self._mode = mode
        self._post_text = post_text_callback
        self._on_disconnect = on_disconnect
        self._ws: Any = None
        self._connected = False
        self._closing = False
        self._disconnect_fired = False
        self._audio_out_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._receive_task: asyncio.Task | None = None
        self._responding = False
        self._response_ended_at: float = 0.0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def suppressing_input(self) -> bool:
        """True while Grok is speaking or within the post-response cooldown."""
        if self._responding:
            return True
        if self._response_ended_at and (
            time.monotonic() - self._response_ended_at < ECHO_SUPPRESSION_COOLDOWN_SEC
        ):
            return True
        return False

    async def start(self) -> None:
        self._responding = False
        self._response_ended_at = 0.0
        extra_headers = {"Authorization": f"Bearer {self._api_key}"}
        self._ws = await websockets.connect(
            GROK_VOICE_URL,
            additional_headers=extra_headers,
            max_size=2**22,
        )
        self._connected = True

        instructions, tools, greeting = get_session_config(self._mode)

        await self._ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "voice": self._voice,
                "instructions": instructions,
                "turn_detection": {"type": "server_vad"},
                "audio": {
                    "input": {"format": {"type": "audio/pcm", "rate": 16000}},
                    "output": {"format": {"type": "audio/pcm", "rate": 24000}},
                },
                "tools": tools,
            },
        }))

        self._receive_task = asyncio.create_task(self._receive_loop())
        log.info("Grok Voice session connected (voice=%s, mode=%s)", self._voice, self._mode)

        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": greeting}],
            },
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

        if self._mode != STORY:
            self._emit_mode_session_record()

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Forward 16kHz mono PCM from Discord to Grok.

        Silently drops frames while Grok is responding (or within the
        post-response cooldown) to prevent the echo-feedback loop that
        causes double responses.
        """
        if not self._ws or not self._connected:
            return
        if self.suppressing_input:
            return
        try:
            b64 = base64.b64encode(pcm_bytes).decode("ascii")
            await self._ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": b64,
            }))
        except Exception:
            log.exception("Failed to send audio to Grok")
            self._connected = False
            asyncio.create_task(self._signal_disconnect("send_audio failed"))

    async def get_audio(self) -> bytes | None:
        """Pull next 24kHz mono PCM chunk for Discord playback.

        Returns None when the session has closed, signaling the drain
        task to exit cleanly instead of hanging forever.
        """
        while self._connected:
            try:
                data = await asyncio.wait_for(
                    self._audio_out_queue.get(), timeout=2.0
                )
                return data if data else None
            except asyncio.TimeoutError:
                continue
        return None

    async def close(self) -> None:
        self._closing = True
        self._connected = False
        self._responding = False
        try:
            self._audio_out_queue.put_nowait(b"")
        except asyncio.QueueFull:
            pass
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        log.info("Grok Voice session closed")

    def _emit_session_record(
        self,
        preferences: str,
        kinks: list[str],
        user_name: str,
        outline: str,
        continue_previous: bool,
    ) -> None:
        """Write a session record for the SpicyLit story-mode correctness harness."""
        try:
            from src.db import record_session
            record_session(
                session_key=f"spicylit-{self._user_id}",
                tool_name="spicylit_generate_outline",
                inputs={
                    "args": {
                        "preferences": preferences,
                        "kinks": kinks,
                        "user_name": user_name,
                        "continue_previous": continue_previous,
                        "mode": self._mode,
                    },
                },
                outputs={
                    "result": outline[:10_000],
                    "status": "ok",
                },
            )
        except Exception:
            log.debug("Failed to emit SpicyLit session record", exc_info=True)

    def _emit_mode_session_record(self) -> None:
        """Write a session record for non-story modes (JOI etc.) at session start."""
        try:
            from src.db import record_session
            record_session(
                session_key=f"spicylit-{self._user_id}",
                tool_name=f"spicylit_{self._mode}_session",
                inputs={
                    "args": {"mode": self._mode, "voice": self._voice},
                },
                outputs={
                    "status": "session_started",
                },
            )
        except Exception:
            log.debug("Failed to emit SpicyLit %s session record", self._mode, exc_info=True)

    async def _signal_disconnect(self, reason: str) -> None:
        """Fire the disconnect callback exactly once for unexpected disconnects."""
        if self._disconnect_fired or self._closing:
            return
        self._disconnect_fired = True
        self._connected = False
        try:
            self._audio_out_queue.put_nowait(b"")
        except asyncio.QueueFull:
            pass
        if self._on_disconnect:
            try:
                await self._on_disconnect(reason)
            except Exception:
                log.exception("on_disconnect callback error")

    async def _receive_loop(self) -> None:
        reason = "unknown"
        try:
            async for raw in self._ws:
                if not self._connected:
                    return
                event = json.loads(raw)
                etype = event.get("type", "")

                if etype == "response.output_audio.delta":
                    self._responding = True
                    audio_b64 = event.get("delta", "")
                    if audio_b64:
                        pcm = base64.b64decode(audio_b64)
                        try:
                            self._audio_out_queue.put_nowait(pcm)
                        except asyncio.QueueFull:
                            log.debug("Grok audio queue full — dropping oldest frame")
                            try:
                                self._audio_out_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                pass
                            self._audio_out_queue.put_nowait(pcm)

                elif etype == "response.done":
                    self._responding = False
                    self._response_ended_at = time.monotonic()

                elif etype == "response.function_call_arguments.done":
                    await self._handle_function_call(event)

                elif etype == "error":
                    log.error("Grok Voice error: %s", event)

            reason = "WebSocket stream ended"
        except asyncio.CancelledError:
            log.info("Grok Voice receive loop cancelled")
            return
        except websockets.exceptions.ConnectionClosed as e:
            reason = f"WebSocket closed: {e}"
            log.warning("Grok Voice %s", reason)
        except Exception as e:
            reason = f"receive loop error: {e}"
            log.exception("Grok Voice receive loop error")
        finally:
            self._connected = False
            await self._signal_disconnect(reason)

    async def _handle_function_call(self, event: dict) -> None:
        func_name = event.get("name", "")
        call_id = event.get("call_id", "")
        args_str = event.get("arguments", "{}")

        if func_name != "generate_outline":
            log.warning("Unknown function call: %s", func_name)
            return

        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = {}

        preferences = args.get("preferences", "")
        user_name = args.get("user_name", "You")
        kinks = args.get("kinks", [preferences] if preferences else [])
        continue_previous = args.get("continue_previous", False)

        prior = get_latest_outline(self._user_id) if continue_previous else None

        try:
            result = await generate_outline(
                preferences=preferences,
                user_id=self._user_id,
                api_key=self._api_key,
                user_name=user_name,
                kinks=kinks,
                prior_outline=prior["outline"] if prior else None,
                prior_kinks=prior["kinks"] if prior else None,
                is_continuation=continue_previous and prior is not None,
            )

            save_outline(
                user_id=self._user_id,
                outline=result.outline_text,
                kinks=result.kinks,
                user_name=result.user_name,
            )

            self._emit_session_record(
                preferences=preferences,
                kinks=kinks,
                user_name=user_name,
                outline=result.outline_text,
                continue_previous=continue_previous,
            )

            if self._post_text:
                await self._post_text(
                    f"**SpicyLit Outline**\n\n{result.outline_text}"
                )

            output = json.dumps({
                "outline": result.outline_text,
                "instruction": STORY_POST_OUTLINE_INSTRUCTION,
            })
        except Exception as e:
            log.exception("Outline generation failed")
            output = json.dumps({"error": str(e)})

        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            },
        }))

        # xAI docs: wait for prior turn's audio to finish playing before
        # requesting the next response, otherwise audio overlaps.
        for _ in range(15):
            if self._audio_out_queue.qsize() == 0:
                break
            await asyncio.sleep(0.2)

        await self._ws.send(json.dumps({"type": "response.create"}))
