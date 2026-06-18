#!/usr/bin/env python3
"""Live 'be a user' end-to-end driver.

1. Generates MY OWN voice (Gemini TTS, macOS `say` fallback) for the request
   and saves it as a .wav artifact — proving the voice-generation half works.
2. Drives the LIVE Aria through her real Discord `!ask` user transport (the
   programmatic webhook path -> on_message -> _run_ask -> do_with_claude), so a
   real user request flows through the full refactored pipeline and Aria takes
   a real action: send a uniquely-tokened email to c@c42.io.

Verification (delivery actually landed) is done separately by reading the
audit log + conversation_log for the token. Usage:

    ./.venv/bin/python scripts/e2e_live_user.py <TOKEN>
"""
from __future__ import annotations

import asyncio
import os
import sys
import wave
from datetime import datetime

import aiohttp
from dotenv import load_dotenv

load_dotenv()
from src.config import config  # noqa: E402

API = "https://discord.com/api/v10"


def synth_voice(text: str, out_wav: str) -> str:
    """Generate my own voice for `text`, save 16kHz mono wav. Returns a status."""
    from src.test_audio import synthesize_to_pcm
    for engine in ("gemini", "say"):
        try:
            pcm = synthesize_to_pcm(text, engine=engine)
            with wave.open(out_wav, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16_000)
                w.writeframes(pcm)
            secs = len(pcm) / 2 / 16_000
            return f"engine={engine} bytes={len(pcm)} (~{secs:.1f}s) -> {out_wav}"
        except Exception as e:  # try next engine
            last = f"{engine} failed: {e}"
    return f"TTS unavailable ({last})"


async def main() -> int:
    token = sys.argv[1] if len(sys.argv) > 1 else f"LIVE-{int(datetime.now().timestamp())}"
    bot_token = config.discord_bot_token
    chan = config.discord_text_channel_id
    if not bot_token or not chan:
        print("FATAL: missing DISCORD_APP_BOT_TOKEN / DISCORD_TEXT_CHANNEL_ID")
        return 2

    request_text = (
        f"Send an email to c@c42.io. Subject must be exactly: "
        f"\"Aria live E2E proof {token}\". Body: a short friendly note (2-3 "
        f"sentences) confirming this is an automated end-to-end capability test "
        f"proving Aria can take a user request and act on it, and include the "
        f"current date and time and the token {token}. After sending, reply here "
        f"confirming you sent it and to whom."
    )

    print(f"[1/3] generating my own voice for the request (token={token})")
    status = synth_voice(request_text, "/tmp/aria_user_request.wav")
    print(f"      voice: {status}")

    headers = {"Authorization": f"Bot {bot_token}"}
    async with aiohttp.ClientSession(headers=headers) as s:
        print("[2/3] creating webhook + posting the request as a user (!ask)")
        async with s.post(f"{API}/channels/{chan}/webhooks",
                          json={"name": "aria-e2e-user"}) as r:
            if r.status >= 400:
                print(f"FATAL: webhook create failed: {r.status} {await r.text()}")
                return 3
            wh = await r.json()
        wh_id, wh_tok = wh["id"], wh["token"]
        try:
            async with s.post(
                f"{API}/webhooks/{wh_id}/{wh_tok}?wait=true",
                json={"content": f"!ask {request_text}"},
            ) as r:
                posted = await r.json()
            print(f"      posted !ask (msg id={posted.get('id')}) into channel {chan}")
        finally:
            await s.delete(f"{API}/webhooks/{wh_id}/{wh_tok}")
            print("      webhook deleted")

    print("[3/3] Aria is now processing in a fresh thread; verify via audit/conversation_log")
    print(f"TOKEN={token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
