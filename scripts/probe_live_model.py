#!/usr/bin/env python3
"""Probe a Gemini Live model for voice+tool reliability.

Connects, streams a short synthesized utterance that REQUIRES a tool call, and
reports: throttling (429/503), input transcription, whether a tool_call fired,
and audio/text output. This decides whether a model is viable for Aria's voice
path. Usage: probe_live_model.py [model]
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from src.test_audio import synthesize_to_pcm, chunk_pcm, silence_pcm  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.1-flash-live-preview"

TOOL = types.Tool(function_declarations=[types.FunctionDeclaration(
    name="get_current_time",
    description="Return the current time. Call this whenever asked the time.",
    parameters=types.Schema(type="OBJECT", properties={}),
)])


async def main() -> int:
    client = genai.Client(
        api_key=os.getenv("GEMINI_API_KEY"),
        http_options={"api_version": "v1beta"},
    )
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        tools=[TOOL],
        system_instruction=types.Content(parts=[types.Part(
            text="You are a voice assistant. When asked the time you MUST call get_current_time.")]),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    print(f"[probe] connecting model={MODEL}")
    try:
        async with client.aio.live.connect(model=MODEL, config=cfg) as session:
            print("[probe] connected OK (no setup throttle)")
            pcm = synthesize_to_pcm("What time is it right now? Use your tool to check.", engine="say")
            for ch in chunk_pcm(pcm, chunk_ms=20):
                await session.send_realtime_input(
                    audio=types.Blob(data=ch, mime_type="audio/pcm;rate=16000"))
                await asyncio.sleep(0.004)
            for ch in chunk_pcm(silence_pcm(1000), chunk_ms=20):
                await session.send_realtime_input(
                    audio=types.Blob(data=ch, mime_type="audio/pcm;rate=16000"))
            await session.send_realtime_input(audio_stream_end=True)
            print("[probe] audio sent; awaiting response (30s)...")

            got_tool = False
            audio_bytes = 0
            text_out = ""
            transcript = ""

            async def reader():
                nonlocal got_tool, audio_bytes, text_out, transcript
                async for msg in session.receive():
                    tc = getattr(msg, "tool_call", None)
                    if tc:
                        for fc in tc.function_calls:
                            got_tool = True
                            print(f"[probe] >>> TOOL_CALL: {fc.name}")
                            await session.send_tool_response(
                                function_responses=types.FunctionResponse(
                                    name=fc.name, response={"result": "12:00 PM"}, id=fc.id))
                    sc = getattr(msg, "server_content", None)
                    if sc:
                        it = getattr(sc, "input_transcription", None)
                        if it and it.text:
                            transcript += it.text
                        mt = getattr(sc, "model_turn", None)
                        if mt:
                            for p in mt.parts:
                                idata = getattr(p, "inline_data", None)
                                if idata and idata.data:
                                    audio_bytes += len(idata.data)
                                if getattr(p, "text", None):
                                    text_out += p.text
                        if getattr(sc, "turn_complete", False) and got_tool:
                            return

            await asyncio.wait_for(reader(), timeout=30)
            print(f"[probe] RESULT model={MODEL}")
            print(f"        transcript={transcript!r}")
            print(f"        tool_call={got_tool}  audio_bytes={audio_bytes}  text={text_out!r}")
            return 0 if got_tool else 1
    except Exception as e:
        print(f"[probe] ERROR model={MODEL}: {type(e).__name__}: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
