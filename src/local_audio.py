"""Shared audio plumbing for local Mac mic/speaker pipelines.

Used by both the standalone local_voice entry point and the wake-word path
in bot.py. Owns the speaker output stream and audio format constants.
"""

from __future__ import annotations

import asyncio
import logging
import math
import queue
import time

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

log = logging.getLogger(__name__)

INPUT_RATE = 16_000
OUTPUT_RATE = 24_000
INPUT_BLOCK = 1600   # 100 ms at 16 kHz
OUTPUT_BLOCK = 480   # 20 ms at 24 kHz


class SpeakerOutput:
    """Manages a sounddevice OutputStream that drains Gemini audio to speakers.

    Guards the CoreAudio AUHAL ``-10851`` (kAudioUnitErr_InvalidPropertyValue)
    that fires when the selected output device can't honor 24 kHz mono or its
    config went stale (device switch / wake-from-sleep). The open is guarded,
    PortAudio is re-initialized once, and as a last resort we fall back to the
    device's native samplerate (resampling 24k -> device rate on our side).
    If nothing works we raise loudly — never a silent dead speaker.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=400)
        self._buf = bytearray()
        self._stream: sd.OutputStream | None = None
        self._pump_task: asyncio.Task | None = None
        self.last_output_at: float = 0.0
        self._out_rate: int = OUTPUT_RATE      # actual stream rate (may differ post-fallback)
        self._out_channels: int = 1            # actual stream channels

    def _callback(self, outdata, frames, time_info, status):
        if status:
            log.debug("speaker status: %s", status)
        ch = self._out_channels
        needed = frames * 2 * ch
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
        outdata[:] = samples.reshape(-1, ch)

    def _conform(self, pcm: bytes) -> bytes:
        """Conform Gemini's 24 kHz mono PCM to the actual stream format
        (post-fallback rate/channels). Identity in the common case."""
        if self._out_rate == OUTPUT_RATE and self._out_channels == 1:
            return pcm
        a = np.frombuffer(pcm, dtype=np.int16)
        if self._out_rate != OUTPUT_RATE and a.size:
            g = math.gcd(OUTPUT_RATE, self._out_rate)
            a = resample_poly(a, self._out_rate // g, OUTPUT_RATE // g).astype(np.int16)
        if self._out_channels == 2:
            a = np.repeat(a, 2)  # mono -> duplicated stereo
        return a.tobytes()

    async def _pump(self, gemini) -> None:
        try:
            while gemini.connected:
                pcm = await gemini.get_audio()
                self.last_output_at = time.monotonic()
                pcm = self._conform(pcm)
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

    def _make_stream(self, rate: int, channels: int, blocksize: int) -> sd.OutputStream:
        stream = sd.OutputStream(
            samplerate=rate,
            blocksize=blocksize,
            dtype="int16",
            channels=channels,
            callback=self._callback,
        )
        stream.start()
        self._out_rate = rate
        self._out_channels = channels
        return stream

    def _open_guarded(self) -> sd.OutputStream:
        """Open the speaker stream, surviving AUHAL -10851. Tries 24k mono,
        re-inits PortAudio, then the device's native rate. Loud-fail if all
        fail."""
        # 1) preferred: 24 kHz mono, no resample.
        try:
            return self._make_stream(OUTPUT_RATE, 1, OUTPUT_BLOCK)
        except sd.PortAudioError as e:
            log.warning("speaker open failed at %d Hz mono (%s) — re-initializing CoreAudio", OUTPUT_RATE, e)

        # 2) re-init PortAudio (clears a stale AUHAL after a device switch / sleep) and retry.
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            log.exception("PortAudio re-init failed (continuing to device-native fallback)")
        try:
            return self._make_stream(OUTPUT_RATE, 1, OUTPUT_BLOCK)
        except sd.PortAudioError as e:
            log.warning("speaker still failing at %d Hz after re-init (%s) — trying device-native rate", OUTPUT_RATE, e)

        # 3) device-native samplerate (+ channels), resampling on our side.
        try:
            dev = sd.query_devices(kind="output")
            dev_rate = int(dev.get("default_samplerate") or 48_000)
            dev_ch = 1 if int(dev.get("max_output_channels") or 2) >= 1 else 2
        except Exception:
            dev_rate, dev_ch = 48_000, 2
        block = max(1, int(round(OUTPUT_BLOCK * dev_rate / OUTPUT_RATE)))
        for ch in (dev_ch, 2, 1):
            try:
                stream = self._make_stream(dev_rate, ch, block)
                log.warning("speaker fell back to device-native %d Hz / %d ch (resampling 24k->%d)", dev_rate, ch, dev_rate)
                return stream
            except sd.PortAudioError as e:
                log.warning("device-native open failed at %d Hz / %d ch: %s", dev_rate, ch, e)

        # Loud-fail: no working speaker config. The local path is dead and the
        # caller must hear about it (never a silent no-audio).
        raise RuntimeError(
            f"SpeakerOutput could not open any output stream (AUHAL -10851). "
            f"Tried 24k mono, re-init, and device-native {dev_rate} Hz."
        )

    def start(self, gemini) -> None:
        self._buf.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self.last_output_at = 0.0
        self._out_rate = OUTPUT_RATE
        self._out_channels = 1
        self._stream = self._open_guarded()
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
