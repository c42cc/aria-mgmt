#!/usr/bin/env python3
"""Rigorous voice audibility test — proves a human voice reaches the speaker.

This is the answer to "voice still doesn't work — analyze the audio that comes
through the speaker Aria speaks into and tell me there's a human voice there."

It captures Aria's actual audio at every stage of the outbound path and analyzes
each with: energy (RMS dBFS), ASR transcription (does it say the phrase?), a
"is this a natural human voice?" judgment (Gemini listens to the WAV), and a
saved waveform image. The verdict localizes any break.

Stages
  A  gemini-out      the PCM Gemini Live produces (24 kHz mono)        [standalone]
  B  send-chokepoint the exact bytes Python hands the sidecar          [--live]
  C  post-ffmpeg     after the sidecar's 24k->48k stereo upsample      [standalone]
  D  echo-channel    what the "Echo" bot records FROM the channel      [--live + token]

Run
  # standalone (no Discord needed): proves the source + ffmpeg produce a voice
  .venv/bin/python scripts/voice_audibility_test.py

  # full end-to-end (needs DISCORD_ECHO_BOT_TOKEN — see .env): also B + D
  .venv/bin/python scripts/voice_audibility_test.py --live

Outputs land in voice_test_out/: A.wav B.wav C.wav D.wav, *.png waveforms,
and report.html. The console prints the verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import re
import subprocess
import sys
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from src.config import config  # noqa: E402

OUT_DIR = ROOT / "voice_test_out"
DEFAULT_PHRASE = (
    "Hi, this is Aria. The quick brown fox jumps over the lazy dog, "
    "and a human voice should be clearly audible right now."
)
SILENCE_DBFS = -45.0  # quieter than this for the whole clip => effectively silent
JUDGE_MODEL = "gemini-2.5-flash"


# --------------------------------------------------------------------------- #
# WAV helpers (stdlib `wave`, s16le)
# --------------------------------------------------------------------------- #
def write_wav(path: Path, pcm: bytes, rate: int, channels: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def read_wav(path: Path) -> tuple[np.ndarray, int, int]:
    with wave.open(str(path), "rb") as w:
        ch = w.getnchannels()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype(np.int16)  # mono mixdown for analysis
    return a, rate, ch


def rms_dbfs(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean((samples.astype(np.float64)) ** 2)))
    if rms <= 0:
        return -120.0
    return 20.0 * math.log10(rms / 32768.0)


def save_waveform(path_png: Path, samples: np.ndarray, rate: int, title: str) -> None:
    fig, ax = plt.subplots(figsize=(10, 2.2), dpi=110)
    if samples.size:
        t = np.arange(samples.size) / float(rate)
        ax.plot(t, samples.astype(np.float32) / 32768.0, linewidth=0.4, color="#3b82f6")
        ax.set_xlim(0, max(t[-1], 0.01))
    ax.set_ylim(-1, 1)
    ax.set_title(title, fontsize=10)
    ax.set_xlabel("seconds", fontsize=8)
    ax.set_yticks([-1, 0, 1])
    ax.grid(True, alpha=0.2)
    fig.tight_layout()
    fig.savefig(path_png)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Stage A — capture Gemini Live's audio output for a known phrase
# --------------------------------------------------------------------------- #
async def capture_gemini(phrase: str) -> bytes:
    """Connect to Gemini Live (the pinned voice model) and collect the PCM it
    produces when asked to speak `phrase`. Returns 24 kHz mono s16le bytes."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.google_api_key)
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
            )
        ),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    audio = bytearray()
    async with client.aio.live.connect(model=config.gemini_model, config=cfg) as s:
        await s.send_client_content(
            turns=types.Content(
                role="user",
                parts=[types.Part(text=f"Say this exactly, nothing else: {phrase}")],
            ),
            turn_complete=True,
        )

        async def collect():
            async for msg in s.receive():
                sc = msg.server_content
                if not sc:
                    continue
                if sc.model_turn:
                    for p in sc.model_turn.parts:
                        if p.inline_data and p.inline_data.data:
                            audio.extend(p.inline_data.data)
                if getattr(sc, "turn_complete", False):
                    return

        try:
            await asyncio.wait_for(collect(), timeout=30)
        except asyncio.TimeoutError:
            pass
    return bytes(audio)


# --------------------------------------------------------------------------- #
# Stage C — reproduce the sidecar's exact 24k mono -> 48k stereo ffmpeg
# --------------------------------------------------------------------------- #
def ffmpeg_upsample_24k_mono_to_48k_stereo(pcm24k_mono: bytes) -> bytes:
    """Identical to discord_voice_bridge/index.js ensurePlaybackStream()."""
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0",
            "-ar", "48000", "-ac", "2", "-f", "s16le", "pipe:1",
        ],
        input=pcm24k_mono, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg upsample failed: {proc.stderr.decode()[:300]}")
    return proc.stdout


# --------------------------------------------------------------------------- #
# Gemini audio understanding — ASR + human-voice judgment on a WAV
# --------------------------------------------------------------------------- #
def _gen_audio(prompt: str, wav_bytes: bytes) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.google_api_key)
    resp = client.models.generate_content(
        model=JUDGE_MODEL,
        contents=[
            types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
            prompt,
        ],
    )
    return (resp.text or "").strip()


def transcribe(wav_bytes: bytes) -> str:
    try:
        return _gen_audio(
            "Transcribe the spoken words in this audio EXACTLY. "
            "Output only the transcription, no preamble. If there is no speech, output: [no speech]",
            wav_bytes,
        )
    except Exception as e:
        return f"[ASR error: {type(e).__name__}: {str(e)[:80]}]"


def judge_human(wav_bytes: bytes) -> dict:
    try:
        raw = _gen_audio(
            "Listen to this audio. Is it a natural, intelligible, human-sounding "
            "speaking voice (not silence, not static/noise, not robotic glitching, "
            "not chopped)? Reply in this exact format on one line: "
            "HUMAN=<yes|no> QUALITY=<1-10> NOTE=<short reason>",
            wav_bytes,
        )
    except Exception as e:
        return {"human": "error", "quality": 0, "note": f"{type(e).__name__}: {str(e)[:80]}", "raw": ""}
    human = re.search(r"HUMAN\s*=\s*(\w+)", raw, re.I)
    qual = re.search(r"QUALITY\s*=\s*(\d+)", raw, re.I)
    note = re.search(r"NOTE\s*=\s*(.+)$", raw, re.I)
    return {
        "human": (human.group(1).lower() if human else "?"),
        "quality": (int(qual.group(1)) if qual else 0),
        "note": (note.group(1).strip() if note else raw[:120]),
        "raw": raw,
    }


def phrase_match(transcript: str, phrase: str) -> float:
    norm = lambda s: set(re.findall(r"[a-z]+", s.lower()))
    pw, tw = norm(phrase), norm(transcript)
    if not pw:
        return 0.0
    # ignore very common words so the score reflects content overlap
    stop = {"the", "a", "and", "is", "this", "of", "over", "right", "now"}
    pw2 = pw - stop or pw
    return round(100.0 * len(pw2 & tw) / len(pw2), 1)


# --------------------------------------------------------------------------- #
# Per-stage analysis
# --------------------------------------------------------------------------- #
def analyze_stage(label: str, wav_path: Path, phrase: str, do_judge: bool = True) -> dict:
    samples, rate, ch = read_wav(wav_path)
    dur = samples.size / float(rate) if rate else 0.0
    db = rms_dbfs(samples)
    png = wav_path.with_suffix(".png")
    save_waveform(png, samples, rate, f"{label}  ({rate} Hz, {dur:.1f}s, {db:.1f} dBFS)")

    result = {
        "label": label,
        "wav": str(wav_path),
        "png": str(png),
        "rate": rate,
        "channels": ch,
        "seconds": round(dur, 2),
        "dbfs": round(db, 1),
        "silent": db < SILENCE_DBFS,
        "transcript": "",
        "match_pct": 0.0,
        "human": "skipped",
        "quality": 0,
        "note": "",
    }
    if result["silent"] or not do_judge:
        if result["silent"]:
            result["note"] = "below silence floor — no audio to transcribe"
        return result

    wav_bytes = wav_path.read_bytes()
    tx = transcribe(wav_bytes)
    result["transcript"] = tx
    result["match_pct"] = phrase_match(tx, phrase)
    j = judge_human(wav_bytes)
    result.update(human=j["human"], quality=j["quality"], note=j["note"])
    return result


# --------------------------------------------------------------------------- #
# Live stages (B + D): drive the REAL outbound path + record from the channel
# --------------------------------------------------------------------------- #
async def capture_live(phrase: str, seconds: int) -> tuple[bytes, Path | None, str]:
    """Drive Aria's real outbound path once and capture:
      B = the exact PCM Python sends to the sidecar (via send_tap)
      D = what the Echo recorder hears in the channel (if token present)
    Returns (b_pcm_24k_mono, d_wav_path_or_None, note).
    """
    from src.discord_voice import voice_bridge
    from src.gemini_session import GeminiSession

    channel_id = os.getenv("DISCORD_VOICE_CHANNEL_ID") or config.discord_voice_channel_id
    if not channel_id:
        return b"", None, "DISCORD_VOICE_CHANNEL_ID not set"

    b_buf = bytearray()
    voice_bridge.set_send_tap(lambda pcm: b_buf.extend(pcm))

    echo_token = os.getenv("DISCORD_ECHO_BOT_TOKEN", "")
    d_wav = OUT_DIR / "D.wav"
    recorder = None
    note = ""

    await voice_bridge.start()
    await voice_bridge.join(str(channel_id))

    if echo_token:
        recorder = await asyncio.create_subprocess_exec(
            "node", "recorder.js",
            "--channel", str(channel_id), "--out", str(d_wav), "--seconds", str(seconds + 4),
            cwd=str(ROOT / "discord_voice_bridge"),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "DISCORD_ECHO_BOT_TOKEN": echo_token},
        )
        # wait for Echo to actually be in the channel before Aria speaks
        try:
            await asyncio.wait_for(_await_event(recorder, "joined"), timeout=25)
        except asyncio.TimeoutError:
            note = "Echo recorder did not join in time; D may be empty"
    else:
        note = "DISCORD_ECHO_BOT_TOKEN not set — stage D (channel capture) skipped"

    gem = GeminiSession()
    await gem.connect()
    drain = asyncio.create_task(_drain(gem, voice_bridge))
    await gem.inject_text(f"Say this exactly, nothing else: {phrase}", turn_complete=True)
    await asyncio.sleep(seconds)
    drain.cancel()
    try:
        await drain
    except asyncio.CancelledError:
        pass
    await gem.close()

    if recorder:
        try:
            await asyncio.wait_for(recorder.wait(), timeout=15)
        except asyncio.TimeoutError:
            recorder.kill()

    await voice_bridge.leave()
    voice_bridge.set_send_tap(None)
    await voice_bridge.close()

    have_d = d_wav.exists() and d_wav.stat().st_size > 100
    return bytes(b_buf), (d_wav if have_d else None), note


async def _drain(gem, bridge) -> None:
    try:
        while True:
            pcm = await gem.get_audio()
            await bridge.send_audio(pcm)
    except asyncio.CancelledError:
        pass


async def _await_event(proc, want: str) -> None:
    import json

    while True:
        line = await proc.stdout.readline()
        if not line:
            return
        try:
            evt = json.loads(line.decode())
        except Exception:
            continue
        if evt.get("event") == want:
            return


# --------------------------------------------------------------------------- #
# Verdict + report
# --------------------------------------------------------------------------- #
def render_verdict(stages: dict[str, dict], live: bool) -> tuple[str, str]:
    A = stages.get("A")
    C = stages.get("C")
    D = stages.get("D")

    def ok(s):
        return bool(s) and not s.get("silent") and s.get("human") == "yes"

    if not A or A.get("silent"):
        return ("BREAK: MODEL", "Stage A (Gemini output) is silent — the model produced no audio. "
                "Re-check GEMINI_MODEL / demand-throttling. Transport is irrelevant until A has audio.")
    if not ok(A):
        return ("SOURCE WEAK", f"Stage A has audio ({A['dbfs']} dBFS) but the judge was unsure "
                f"(human={A['human']}, q={A['quality']}). Inspect A.wav / A.png.")
    if C and C.get("silent"):
        return ("BREAK: FFMPEG", "Gemini audio is a clear human voice (A) but the 24k->48k upsample (C) "
                "is silent. The sidecar's ffmpeg step is destroying audio.")
    if not live or D is None:
        return ("SOURCE PROVEN; CHANNEL PENDING",
                "A human voice IS produced by the model and survives the ffmpeg upsample "
                "(stages A + C verified). The channel-delivery stage (D, the Echo recording) "
                "was not captured" + (" (run with --live)" if not live else " (add DISCORD_ECHO_BOT_TOKEN)") +
                ". If you cannot hear Aria, the break is in transport (DAVE encrypt / Opus / UDP), "
                "which D will localize.")
    if D.get("silent"):
        return ("BREAK: TRANSPORT", "A clear human voice is produced (A) and upsampled fine (C), but the "
                "Echo bot recorded SILENCE from the channel (D). The break is the outbound transport: "
                "DAVE encrypt/send (mirror the decrypt patch) or the Opus/UDP path.")
    if ok(D):
        return ("VOICE WORKS", f"The Echo bot recorded a natural human voice from the channel "
                f"(D: {D['dbfs']} dBFS, q={D['quality']}, \"{D['transcript'][:60]}\"). "
                "Aria is audible end-to-end.")
    return ("CHANNEL DEGRADED", f"Echo recorded audio (D: {D['dbfs']} dBFS) but the judge was unsure "
            f"(human={D['human']}). Listen to D.wav.")


def write_report(stages: dict[str, dict], verdict: tuple[str, str], phrase: str) -> Path:
    rows = []
    order = [k for k in ("A", "B", "C", "D") if k in stages]
    for k in order:
        s = stages[k]
        png_rel = Path(s["png"]).name
        rows.append(f"""
        <div class="stage">
          <h3>{s['label']}</h3>
          <div class="meta">{s['rate']} Hz · {s['channels']}ch · {s['seconds']}s ·
            <b>{s['dbfs']} dBFS</b> · {'SILENT' if s['silent'] else 'audio'} ·
            human=<b>{s['human']}</b> q={s['quality']} · match={s['match_pct']}%</div>
          <img src="{png_rel}"/>
          <div class="tx"><b>ASR:</b> {s['transcript'] or '—'}</div>
          <div class="note">{s['note']}</div>
        </div>""")
    html = f"""<!doctype html><meta charset="utf-8"><title>Voice audibility report</title>
<style>
 body{{font:14px -apple-system,system-ui,sans-serif;max-width:920px;margin:30px auto;color:#111;background:#fafafa}}
 .verdict{{padding:16px 20px;border-radius:10px;background:#0f172a;color:#fff;margin-bottom:20px}}
 .verdict h1{{margin:0 0 6px;font-size:20px}}
 .stage{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px 18px;margin:12px 0}}
 .stage h3{{margin:0 0 4px}} .meta{{color:#555;font-size:12px;margin-bottom:8px}}
 img{{width:100%;border:1px solid #eee;border-radius:6px}} .tx{{margin-top:8px}} .note{{color:#777;font-size:12px;margin-top:4px}}
 code{{background:#eef}}
</style>
<div class="verdict"><h1>{verdict[0]}</h1><div>{verdict[1]}</div>
 <div style="margin-top:10px;opacity:.7;font-size:12px">phrase: “{phrase}” · {datetime.now():%Y-%m-%d %H:%M}</div></div>
{''.join(rows)}
"""
    out = OUT_DIR / "report.html"
    out.write_text(html)
    return out


# --------------------------------------------------------------------------- #
async def main_async(args) -> int:
    OUT_DIR.mkdir(exist_ok=True)
    phrase = args.phrase
    stages: dict[str, dict] = {}

    print(f"\n=== VOICE AUDIBILITY TEST ===\nphrase: {phrase!r}\nmodel:  {config.gemini_model}\n")

    # Stage A — Gemini output
    print("[A] capturing Gemini Live output…")
    a_pcm = await capture_gemini(phrase)
    a_wav = OUT_DIR / "A.wav"
    write_wav(a_wav, a_pcm, rate=24000, channels=1)
    stages["A"] = analyze_stage("A · Gemini output (24k mono)", a_wav, phrase)
    print(f"    {len(a_pcm)} bytes -> {stages['A']['dbfs']} dBFS, human={stages['A']['human']}, "
          f"asr={stages['A']['transcript'][:50]!r}")

    # Stage C — post-ffmpeg (reproduced from the same PCM)
    if a_pcm:
        print("[C] reproducing sidecar ffmpeg 24k->48k stereo…")
        c_pcm = ffmpeg_upsample_24k_mono_to_48k_stereo(a_pcm)
        c_wav = OUT_DIR / "C.wav"
        write_wav(c_wav, c_pcm, rate=48000, channels=2)
        stages["C"] = analyze_stage("C · post-ffmpeg (48k stereo)", c_wav, phrase)
        print(f"    {len(c_pcm)} bytes -> {stages['C']['dbfs']} dBFS, human={stages['C']['human']}")

    # Stages B + D — the real path + the channel (needs Discord)
    if args.live:
        print("[B/D] driving the real outbound path + Echo recorder…")
        try:
            b_pcm, d_wav, note = await capture_live(phrase, seconds=args.seconds)
            if note:
                print(f"    note: {note}")
            if b_pcm:
                b_wav = OUT_DIR / "B.wav"
                write_wav(b_wav, b_pcm, rate=24000, channels=1)
                stages["B"] = analyze_stage("B · send chokepoint (24k mono)", b_wav, phrase)
                print(f"    [B] {len(b_pcm)} bytes -> {stages['B']['dbfs']} dBFS, human={stages['B']['human']}")
            if d_wav:
                stages["D"] = analyze_stage("D · Echo channel recording", d_wav, phrase)
                print(f"    [D] {stages['D']['seconds']}s -> {stages['D']['dbfs']} dBFS, "
                      f"human={stages['D']['human']}, asr={stages['D']['transcript'][:50]!r}")
        except Exception as e:
            print(f"    live capture failed: {type(e).__name__}: {e}")
    else:
        print("[B/D] skipped (standalone mode). Use --live for the channel capture.")

    verdict = render_verdict(stages, args.live)
    report = write_report(stages, verdict, phrase)

    print("\n" + "=" * 64)
    print(f"VERDICT: {verdict[0]}")
    print(verdict[1])
    print("=" * 64)
    print(f"\nArtifacts in {OUT_DIR}/:")
    for k in ("A", "B", "C", "D"):
        if k in stages:
            print(f"  {k}: {Path(stages[k]['wav']).name}  +  {Path(stages[k]['png']).name}")
    print(f"  report: {report}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Prove a human voice reaches Aria's speaker.")
    ap.add_argument("--phrase", default=DEFAULT_PHRASE)
    ap.add_argument("--live", action="store_true",
                    help="Also drive the real Discord path + Echo recorder (stages B, D).")
    ap.add_argument("--seconds", type=int, default=16, help="Live capture window.")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
