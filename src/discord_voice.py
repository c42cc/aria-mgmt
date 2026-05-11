"""Voice channel I/O and PCM resampling between Discord and Gemini."""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

DISCORD_SAMPLE_RATE = 48_000
DISCORD_CHANNELS = 2
GEMINI_SAMPLE_RATE = 16_000
GEMINI_CHANNELS = 1


def discord_to_gemini(pcm_bytes: bytes) -> bytes:
    """Resample 48kHz stereo PCM (int16) → 16kHz mono PCM (int16)."""
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    # Stereo to mono: average channels
    if DISCORD_CHANNELS == 2:
        samples = samples.reshape(-1, 2).mean(axis=1).astype(np.int16)
    # Downsample 48k → 16k (take every 3rd sample)
    ratio = DISCORD_SAMPLE_RATE // GEMINI_SAMPLE_RATE
    samples = samples[::ratio]
    return samples.tobytes()


def gemini_to_discord(pcm_bytes: bytes) -> bytes:
    """Resample 16kHz mono PCM (int16) → 48kHz stereo PCM (int16)."""
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    # Upsample 16k → 48k (repeat each sample 3x)
    ratio = DISCORD_SAMPLE_RATE // GEMINI_SAMPLE_RATE
    samples = np.repeat(samples, ratio)
    # Mono to stereo: duplicate channel
    stereo = np.column_stack((samples, samples)).flatten().astype(np.int16)
    return stereo.tobytes()
