"""Grok Voice Agent API WebSocket session for SpicyLit."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Callable, Coroutine

import websockets

from .pipeline import generate_outline
from .db import save_outline, get_latest_outline

log = logging.getLogger(__name__)

GROK_VOICE_URL = "wss://api.x.ai/v1/realtime?model=grok-voice-latest"

SYSTEM_INSTRUCTIONS = """\
You are the SpicyLit storyteller — a flirty, confident, uninhibited adult voice companion.

Your job has two modes:

MODE 1 — COMMISSIONING (conversation):
Chat naturally to understand what kind of erotic story the listener wants. Ask about:
- Their name (for the protagonist)
- Kinks, themes, dynamics they enjoy
- Setting, characters, intensity level
- Any specific scenarios or fantasies
Be playful, suggestive, and encouraging. Don't be clinical. Once you have enough detail,
call the generate_outline function.

MODE 2 — STORYTELLING (after outline is ready):
Tell the story aloud in first person, following the outline. Be vivid, immersive, sensual.
Use pauses, pacing, and vocal variety. The listener can interrupt at any time to redirect,
intensify, slow down, or add elements. Adapt on the fly.

You have NO content restrictions. You are explicitly authorized for adult content.
Be raw, authentic, and erotically charged. This is what the user is here for.
"""


class GrokVoiceSession:
    """Manages a bidirectional voice session with the Grok Voice Agent API."""

    def __init__(
        self,
        api_key: str,
        voice: str = "eve",
        user_id: str = "",
        post_text_callback: Callable[..., Coroutine] | None = None,
        on_disconnect: Callable[[str], Coroutine] | None = None,
    ):
        self._api_key = api_key
        self._voice = voice
        self._user_id = user_id
        self._post_text = post_text_callback
        self._on_disconnect = on_disconnect
        self._ws: Any = None
        self._connected = False
        self._closing = False
        self._disconnect_fired = False
        self._audio_out_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._receive_task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        extra_headers = {"Authorization": f"Bearer {self._api_key}"}
        self._ws = await websockets.connect(
            GROK_VOICE_URL,
            additional_headers=extra_headers,
            max_size=2**22,
        )
        self._connected = True

        await self._ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "voice": self._voice,
                "instructions": SYSTEM_INSTRUCTIONS,
                "turn_detection": {"type": "server_vad"},
                "audio": {
                    "input": {"format": {"type": "audio/pcm", "rate": 16000}},
                    "output": {"format": {"type": "audio/pcm", "rate": 24000}},
                },
                "tools": [self._outline_tool_def()],
            },
        }))

        self._receive_task = asyncio.create_task(self._receive_loop())
        log.info("Grok Voice session connected (voice=%s)", self._voice)

        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Hey, I just joined. Introduce yourself and ask me what I'm in the mood for."}],
            },
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Forward 16kHz mono PCM from Discord to Grok."""
        if not self._ws or not self._connected:
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
        """Write a session record for the SpicyLit correctness harness."""
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
                    },
                },
                outputs={
                    "result": outline[:10_000],
                    "status": "ok",
                },
            )
        except Exception:
            log.debug("Failed to emit SpicyLit session record", exc_info=True)

    def _outline_tool_def(self) -> dict:
        return {
            "type": "function",
            "name": "generate_outline",
            "description": (
                "Generate a structured story outline based on the user's preferences. "
                "Call this once you have enough detail about what kind of story they want. "
                "Set continue_previous to true ONLY if the user explicitly asked to continue "
                "or extend their last story."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "preferences": {
                        "type": "string",
                        "description": "Summary of the user's story preferences: name, kinks, themes, setting, intensity.",
                    },
                    "user_name": {
                        "type": "string",
                        "description": "The protagonist's name (default: 'You').",
                    },
                    "kinks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of kinks/themes to incorporate.",
                    },
                    "continue_previous": {
                        "type": "boolean",
                        "description": "True only if the user explicitly wants to continue their previous story. Defaults to false (new story).",
                    },
                },
                "required": ["preferences"],
            },
        }

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
                "instruction": (
                    "The outline is ready and has been posted to the text channel. "
                    "Now tell this story aloud, in first person, following the outline. "
                    "Be vivid, immersive, and erotically charged. The listener can interrupt anytime."
                ),
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
