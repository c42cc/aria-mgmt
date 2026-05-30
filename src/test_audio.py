"""Reusable text-to-PCM utility for autonomous voice testing.

Synthesizes text into 16 kHz mono s16le PCM bytes — exactly the format
`GeminiSession.send_audio()` expects (`audio/pcm;rate=16000`). The
`scripts/e2e_aria_golden.py --tts` mode uses this to drive Aria's voice
path WITHOUT requiring a human in the Discord voice channel:

  test text -> synthesize_to_pcm() -> 16 kHz PCM
            -> CursorExternalObserver /test_voice_in
            -> gemini.send_audio(pcm chunks)
            -> Gemini Live transcribes
            -> tool dispatch + reply

Two engines:

- "gemini" (default) — Gemini 2.5 Flash TTS via the `google-genai`
  Python SDK. Uses `GEMINI_API_KEY`. Produces 24 kHz mono s16le, which
  we downsample to 16 kHz via `audioop.ratecv` (or scipy as a fallback
  once audioop is removed in 3.13+). High quality; Gemini Live's
  transcriber handles it cleanly.

- "say" — macOS `say` + `afconvert`. Free, on-device, no API. Lower
  quality but useful when offline or when GEMINI_API_KEY is exhausted.

Module is import-safe. The synthesize_to_pcm() call is sync; callers
on an event loop should wrap it with `asyncio.to_thread`. The Gemini
TTS call typically takes 1-4 s for a one-sentence utterance.

USAGE
  from src.test_audio import synthesize_to_pcm, chunk_pcm
  pcm = synthesize_to_pcm("What did we do last?")
  for chunk in chunk_pcm(pcm, chunk_ms=20):
      await gemini.send_audio(chunk)
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import tempfile
import wave
from typing import Iterator

log = logging.getLogger(__name__)

TARGET_RATE_HZ = 16_000
TARGET_SAMPLE_WIDTH = 2  # 16 bit
TARGET_CHANNELS = 1

# Gemini TTS endpoint conventions (as of 2026-05).
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_TTS_VOICE_DEFAULT = "Kore"   # neutral female; transcribes cleanly
GEMINI_TTS_SAMPLE_RATE_HZ = 24_000  # what the model returns


class TTSError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def synthesize_to_pcm(
    text: str, *,
    engine: str = "gemini",
    voice: str = GEMINI_TTS_VOICE_DEFAULT,
) -> bytes:
    """Return 16 kHz mono s16le PCM bytes for `text`.

    Choose `engine="say"` to skip the network round-trip; useful for
    offline runs, CI, or when the Gemini API is rate-limited.
    """
    body = text.strip()
    if not body:
        raise TTSError("synthesize_to_pcm: empty text")

    if engine == "gemini":
        try:
            return _synthesize_gemini(body, voice=voice)
        except Exception as exc:
            log.warning(
                "Gemini TTS failed (%s) — falling back to macOS say",
                exc,
            )
            return _synthesize_say(body)
    if engine == "say":
        return _synthesize_say(body)
    raise TTSError(f"unknown engine: {engine!r}")


def chunk_pcm(pcm: bytes, *, chunk_ms: int = 20, sample_rate_hz: int = TARGET_RATE_HZ) -> Iterator[bytes]:
    """Yield `pcm` in chunk_ms-sized pieces.

    Default 20 ms at 16 kHz mono s16le = 640 bytes per chunk, which
    matches what the production Discord voice bridge feeds Aria. The
    final chunk may be shorter than chunk_ms.
    """
    bytes_per_sec = sample_rate_hz * TARGET_SAMPLE_WIDTH * TARGET_CHANNELS
    chunk_bytes = max(1, (bytes_per_sec * chunk_ms) // 1000)
    # Round chunk_bytes down to an even multiple of sample width to avoid
    # cutting a sample in half.
    chunk_bytes -= chunk_bytes % TARGET_SAMPLE_WIDTH
    if chunk_bytes <= 0:
        chunk_bytes = TARGET_SAMPLE_WIDTH
    for i in range(0, len(pcm), chunk_bytes):
        yield pcm[i:i + chunk_bytes]


def silence_pcm(duration_ms: int, sample_rate_hz: int = TARGET_RATE_HZ) -> bytes:
    """Return `duration_ms` of mono s16le silence at `sample_rate_hz`."""
    samples = max(0, (sample_rate_hz * duration_ms) // 1000)
    return b"\x00\x00" * samples


# ---------------------------------------------------------------------------
# Gemini TTS engine
# ---------------------------------------------------------------------------

def _synthesize_gemini(text: str, *, voice: str) -> bytes:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise TTSError("GEMINI_API_KEY not set in environment")

    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise TTSError(f"google-genai SDK not installed: {e}") from e

    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=GEMINI_TTS_MODEL,
        contents=text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice),
                )
            ),
        ),
    )

    raw: bytes | None = None
    for cand in resp.candidates or []:
        for part in cand.content.parts or []:
            data = getattr(part, "inline_data", None)
            if data is not None and data.data:
                raw = data.data
                break
        if raw is not None:
            break

    if raw is None:
        raise TTSError("Gemini TTS returned no audio bytes")

    return _resample_24k_to_16k(raw)


def _resample_24k_to_16k(pcm_24k_s16: bytes) -> bytes:
    """Downsample 24 kHz mono s16le to 16 kHz mono s16le.

    Uses `audioop.ratecv` (built-in, low-latency, lossy linear).
    Falls back to `scipy.signal.resample_poly` if audioop is unavailable
    (Python 3.13+).
    """
    try:
        import audioop  # type: ignore[import-not-found]
        out, _state = audioop.ratecv(pcm_24k_s16, 2, 1, 24_000, 16_000, None)
        return out
    except ImportError:
        pass

    try:
        import numpy as np
        from scipy.signal import resample_poly
    except ImportError as e:
        raise TTSError("neither audioop nor scipy available for resample") from e

    arr = np.frombuffer(pcm_24k_s16, dtype=np.int16)
    # 24 kHz -> 16 kHz: factor 2/3
    resampled = resample_poly(arr.astype(np.float32), up=2, down=3)
    clipped = np.clip(resampled, -32768, 32767).astype(np.int16)
    return clipped.tobytes()


# ---------------------------------------------------------------------------
# macOS `say` engine
# ---------------------------------------------------------------------------

def _synthesize_say(text: str) -> bytes:
    if not shutil.which("say") or not shutil.which("afconvert"):
        raise TTSError("macOS `say`/`afconvert` not on PATH")

    with tempfile.TemporaryDirectory() as tmp:
        aiff_path = os.path.join(tmp, "utt.aiff")
        wav_path = os.path.join(tmp, "utt.wav")

        proc = subprocess.run(
            ["say", "-o", aiff_path, "-r", "180", text],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise TTSError(f"`say` failed: {proc.stderr[:200]}")

        # Convert to 16 kHz mono s16le WAV.
        proc = subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
             aiff_path, wav_path],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise TTSError(f"`afconvert` failed: {proc.stderr[:200]}")

        # Strip the WAV header.
        with wave.open(wav_path, "rb") as wf:
            if wf.getframerate() != TARGET_RATE_HZ:
                raise TTSError(
                    f"`afconvert` produced wrong rate: {wf.getframerate()} != {TARGET_RATE_HZ}"
                )
            if wf.getnchannels() != 1:
                raise TTSError(f"`afconvert` produced non-mono audio: {wf.getnchannels()}ch")
            if wf.getsampwidth() != TARGET_SAMPLE_WIDTH:
                raise TTSError(f"`afconvert` produced wrong sample width: {wf.getsampwidth()}")
            n_frames = wf.getnframes()
            return wf.readframes(n_frames)


# ---------------------------------------------------------------------------
# CLI for ad-hoc use / debugging
# ---------------------------------------------------------------------------

def _main() -> int:
    import argparse
    # Load .env when invoked as a CLI so GEMINI_API_KEY is available
    # without the caller having to source it manually.
    try:
        from dotenv import load_dotenv
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env",
        )
        if os.path.exists(env_path):
            load_dotenv(env_path)
    except ImportError:
        pass

    ap = argparse.ArgumentParser(
        description="Synthesize text to 16 kHz mono s16le PCM (for use with gemini.send_audio).",
    )
    ap.add_argument("text", help="Text to synthesize")
    ap.add_argument("--engine", choices=["gemini", "say"], default="gemini")
    ap.add_argument("--voice", default=GEMINI_TTS_VOICE_DEFAULT,
                    help="Gemini TTS voice name (default: Kore)")
    ap.add_argument("--out", default="/tmp/tts_out.pcm",
                    help="Output PCM file (default: /tmp/tts_out.pcm)")
    ap.add_argument("--wav", default="",
                    help="Also write a playable WAV at this path")
    ap.add_argument("--play", action="store_true",
                    help="Play the result via afplay (macOS)")
    args = ap.parse_args()

    pcm = synthesize_to_pcm(args.text, engine=args.engine, voice=args.voice)
    print(f"synthesized {len(pcm)} bytes "
          f"({len(pcm) / (TARGET_RATE_HZ * TARGET_SAMPLE_WIDTH):.2f}s @ {TARGET_RATE_HZ} Hz mono s16le)")

    with open(args.out, "wb") as f:
        f.write(pcm)
    print(f"wrote PCM -> {args.out}")

    wav_path = args.wav
    if args.play and not wav_path:
        wav_path = args.out + ".wav"
    if wav_path:
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(TARGET_RATE_HZ)
            wf.writeframes(pcm)
        print(f"wrote WAV -> {wav_path}")
        if args.play:
            subprocess.run(["afplay", wav_path], check=False)

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
