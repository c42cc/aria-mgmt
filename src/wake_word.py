"""On-device wake-word detection using OpenWakeWord + sounddevice.

Runs a continuous 16 kHz mic stream. In listening mode, feeds frames to
OpenWakeWord. When a wake word exceeds the threshold, fires the on_wake
callback. In paused mode (during an active Gemini session), optionally
forwards raw PCM to a callback instead of running detection.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import time
from typing import Awaitable, Callable

import numpy as np
import sounddevice as sd

from .local_audio import INPUT_RATE, INPUT_BLOCK

log = logging.getLogger(__name__)

DETECTION_THRESHOLD = 0.5
COOLDOWN_SEC = 2.0


class WakeWordListener:
    """Listens for a wake word on the Mac mic and fires a callback.

    Lifecycle::

        listener = WakeWordListener(on_wake=my_handler)
        await listener.start()          # opens mic, begins detection
        listener.pause()                # stop detection, keep mic for forwarding
        listener.set_forward_callback(gemini.send_audio)
        ...
        listener.set_forward_callback(None)
        listener.resume()               # re-enable detection
        await listener.stop()           # tear down
    """

    def __init__(
        self,
        on_wake: Callable[[], Awaitable[None]],
        models: list[str] | None = None,
        threshold: float = DETECTION_THRESHOLD,
    ) -> None:
        self._on_wake = on_wake
        self._model_names = models or ["hey_jarvis"]
        self._threshold = threshold
        self._stream: sd.InputStream | None = None
        self._paused = False
        self._audio_forward: Callable[[bytes], Awaitable[None]] | None = None
        self._frame_queue: queue.Queue[bytes] = queue.Queue(maxsize=200)
        self._task: asyncio.Task | None = None
        self._oww_model = None
        self._running = False

    def _load_model(self) -> None:
        from openwakeword.model import Model
        from openwakeword.utils import download_models
        download_models(self._model_names)
        self._oww_model = Model(
            wakeword_models=self._model_names,
            inference_framework="onnx",
        )
        log.info("OpenWakeWord models loaded: %s", self._model_names)

    def _mic_callback(self, indata, frames, time_info, status):
        if status:
            log.debug("wake mic status: %s", status)
        pcm = indata.tobytes()
        try:
            self._frame_queue.put_nowait(pcm)
        except queue.Full:
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._frame_queue.put_nowait(pcm)
            except queue.Full:
                pass

    async def start(self) -> None:
        if self._running:
            return
        self._load_model()
        self._stream = sd.InputStream(
            samplerate=INPUT_RATE,
            blocksize=INPUT_BLOCK,
            dtype="int16",
            channels=1,
            callback=self._mic_callback,
        )
        self._stream.start()
        self._running = True
        self._task = asyncio.create_task(self._detection_loop(), name="wake_word_loop")
        log.info(
            "Wake-word listener started (models=%s, threshold=%.2f)",
            self._model_names,
            self._threshold,
        )

    async def _detection_loop(self) -> None:
        last_wake = 0.0
        try:
            while self._running:
                try:
                    pcm = await asyncio.to_thread(self._frame_queue.get, True, 0.5)
                except queue.Empty:
                    continue

                if self._audio_forward:
                    try:
                        await self._audio_forward(pcm)
                    except Exception:
                        log.debug("audio forward error", exc_info=True)

                if self._paused:
                    continue

                audio_array = np.frombuffer(pcm, dtype=np.int16)
                prediction = self._oww_model.predict(audio_array)

                for model_name, score in prediction.items():
                    if score >= self._threshold:
                        now = time.monotonic()
                        if now - last_wake < COOLDOWN_SEC:
                            continue
                        last_wake = now
                        log.info("Wake word detected: %s (score=%.3f)", model_name, score)
                        self._oww_model.reset()
                        try:
                            await self._on_wake()
                        except Exception:
                            log.exception("on_wake callback failed")
                        break
        except asyncio.CancelledError:
            pass

    def pause(self) -> None:
        """Pause wake-word detection. Mic keeps streaming for forwarding."""
        self._paused = True

    def resume(self) -> None:
        """Resume wake-word detection."""
        if self._oww_model:
            self._oww_model.reset()
        self._paused = False

    def set_forward_callback(
        self, cb: Callable[[bytes], Awaitable[None]] | None
    ) -> None:
        """When set, raw mic PCM is also forwarded to this callback."""
        self._audio_forward = cb

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    @property
    def model_loaded(self) -> bool:
        return self._oww_model is not None
