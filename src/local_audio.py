"""Shared audio plumbing for local Mac mic/speaker pipelines.

Used by both the standalone local_voice entry point and the wake-word path
in bot.py. Owns the speaker output stream and audio format constants.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import time

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

INPUT_RATE = 16_000
OUTPUT_RATE = 24_000
INPUT_BLOCK = 1600   # 100 ms at 16 kHz
OUTPUT_BLOCK = 480   # 20 ms at 24 kHz


class SpeakerOutput:
    """Manages a sounddevice OutputStream that drains Gemini audio to speakers."""

    def __init__(self) -> None:
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=400)
        self._buf = bytearray()
        self._stream: sd.OutputStream | None = None
        self._pump_task: asyncio.Task | None = None
        self.last_output_at: float = 0.0

    def _callback(self, outdata, frames, time_info, status):
        if status:
            log.debug("speaker status: %s", status)
        needed = frames * 2
        while len(self._buf) < needed:
            try:
                chunk = self._queue.get_nowait()
            except queue.Empty:
                break
            self._buf.extend(chunk)
        if len(self._buf) >= needed:
            chunk_bytes = bytes(self._buf[:needed])
            del self._buf[:needed]
        else:
            chunk_bytes = bytes(self._buf) + b"\x00" * (needed - len(self._buf))
            self._buf.clear()
        samples = np.frombuffer(chunk_bytes, dtype=np.int16)
        outdata[:] = samples.reshape(-1, 1)

    async def _pump(self, gemini) -> None:
        try:
            while gemini.connected:
                pcm = await gemini.get_audio()
                self.last_output_at = time.monotonic()
                try:
                    self._queue.put_nowait(pcm)
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._queue.put_nowait(pcm)
                    except queue.Full:
                        pass
        except asyncio.CancelledError:
            pass

    def start(self, gemini) -> None:
        self._buf.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self.last_output_at = 0.0
        self._stream = sd.OutputStream(
            samplerate=OUTPUT_RATE,
            blocksize=OUTPUT_BLOCK,
            dtype="int16",
            channels=1,
            callback=self._callback,
        )
        self._stream.start()
        self._pump_task = asyncio.create_task(self._pump(gemini), name="speaker_pump")

    async def stop(self) -> None:
        if self._pump_task:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
