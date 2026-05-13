"""Local voice loop. Mic + speakers + Gemini Live + full tool surface. No Discord."""

from __future__ import annotations

import asyncio
import logging
import queue
import signal
import sys
import time

import numpy as np
import sounddevice as sd

from .config import config
from .cursor_bridge import CursorBridge
from .db import init_db, update_cursor_session_event, upsert_cursor_session
from .gemini_session import GeminiSession
from .memory import init_memory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("local_voice")

# Gemini Live audio formats: 16-bit signed PCM, mono, little-endian
INPUT_RATE = 16000   # Gemini expects 16 kHz mono PCM
OUTPUT_RATE = 24000  # Gemini emits 24 kHz mono PCM
INPUT_BLOCK = 1600   # 100 ms at 16 kHz
OUTPUT_BLOCK = 480   # 20 ms at 24 kHz (lower playback latency)


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

cursor_bridge = CursorBridge()
gemini: GeminiSession | None = None

# Thread-safe queues bridging portaudio callbacks (other thread) and asyncio
_mic_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=200)
_speaker_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=400)
_speaker_buf = bytearray()


# ---------------------------------------------------------------------------
# Stdout-routed tool callbacks (replace Discord text channels)
# ---------------------------------------------------------------------------

async def _post_stdout(content: str, thread=None) -> None:
    print(f"\n[aria] {content}\n", flush=True)


async def _alert_stdout(content: str) -> None:
    print(f"\n[!] {content}\n", flush=True)


async def _cursor_event_consumer_stdout(session_id: str, thread=None) -> None:
    """Stream Cursor build events to stdout and inject status into Gemini."""
    last_print = 0.0
    try:
        async for event in cursor_bridge.read_events(session_id):
            etype = event.get("event", "")
            data = event.get("data", {})

            if etype in ("file_edit", "test_run"):
                now = time.monotonic()
                if now - last_print > 5:
                    summary = data.get("summary", etype)
                    print(f"  [cursor:{session_id[:8]} {etype}] {str(summary)[:200]}", flush=True)
                    last_print = now
                update_cursor_session_event(session_id, etype)

            elif etype == "question" and gemini:
                question = data.get("text", "Cursor has a question")
                await gemini.inject_text(f"Cursor is asking: {question}", turn_complete=True)

            elif etype == "completion":
                print(f"  [cursor:{session_id[:8]} done]", flush=True)
                if gemini and gemini.connected:
                    await gemini.inject_text("The Cursor build has completed.", turn_complete=True)
                upsert_cursor_session(session_id, "", status="completed")
                break

            elif etype == "error":
                msg = data.get("message", "Unknown error")
                print(f"  [cursor:{session_id[:8]} ERROR] {msg}", flush=True)
                upsert_cursor_session(session_id, "", status="error")
                break
    except Exception:
        log.exception("Cursor event consumer error for %s", session_id)
    finally:
        cursor_bridge.close_session(session_id)


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

async def _handle_tool_call(name: str, args: dict) -> str:
    from .tools import handle_tool_call
    return await handle_tool_call(name, args)


# ---------------------------------------------------------------------------
# Audio plumbing
# ---------------------------------------------------------------------------

def _mic_callback(indata, frames, time_info, status):
    """sounddevice input callback (runs in a portaudio thread)."""
    if status:
        log.debug("mic status: %s", status)
    pcm = indata.tobytes()
    try:
        _mic_queue.put_nowait(pcm)
    except queue.Full:
        try:
            _mic_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            _mic_queue.put_nowait(pcm)
        except queue.Full:
            pass


def _speaker_callback(outdata, frames, time_info, status):
    """sounddevice output callback (runs in a portaudio thread)."""
    if status:
        log.debug("speaker status: %s", status)
    needed = frames * 2  # int16 mono = 2 bytes per frame
    while len(_speaker_buf) < needed:
        try:
            chunk = _speaker_queue.get_nowait()
        except queue.Empty:
            break
        _speaker_buf.extend(chunk)
    if len(_speaker_buf) >= needed:
        chunk_bytes = bytes(_speaker_buf[:needed])
        del _speaker_buf[:needed]
    else:
        chunk_bytes = bytes(_speaker_buf) + b"\x00" * (needed - len(_speaker_buf))
        _speaker_buf.clear()
    samples = np.frombuffer(chunk_bytes, dtype=np.int16)
    outdata[:] = samples.reshape(-1, 1)


async def _mic_pump() -> None:
    """Drain mic queue and ship PCM to Gemini."""
    try:
        while gemini and gemini.connected:
            try:
                pcm = await asyncio.to_thread(_mic_queue.get, True, 0.5)
            except queue.Empty:
                continue
            if pcm and gemini and gemini.connected:
                await gemini.send_audio(pcm)
    except asyncio.CancelledError:
        pass


async def _speaker_pump() -> None:
    """Drain Gemini's audio output into the speaker queue."""
    try:
        while gemini and gemini.connected:
            pcm = await gemini.get_audio()
            try:
                _speaker_queue.put_nowait(pcm)
            except queue.Full:
                try:
                    _speaker_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    _speaker_queue.put_nowait(pcm)
                except queue.Full:
                    pass
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    global gemini

    if not config.google_api_key:
        log.error("GEMINI_API_KEY is not set. Add it to .env and re-run.")
        return 1
    if not config.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY is not set — Claude-backed tools will fail.")

    log.info("Starting local voice mode (no Discord).")

    init_db()
    try:
        init_memory()
    except Exception:
        log.exception("Memory init failed — continuing without long-term memory.")

    from .tools import init_tools
    init_tools(
        cursor_bridge=cursor_bridge,
        post_callback=_post_stdout,
        alert_callback=_alert_stdout,
        thread_callback=None,
        cursor_event_callback=_cursor_event_consumer_stdout,
    )

    await cursor_bridge.start()
    log.info("Cursor bridge ready.")

    mcp = None
    from .mcp import init_mcp
    try:
        mcp = await init_mcp()
        log.info("MCP fleet started.")
    except Exception:
        log.exception("MCP failed to start — continuing without it.")

    gemini = GeminiSession(tool_handler=_handle_tool_call)

    if mcp:
        async def _confirm_cb(action_id: str, tool_name: str, summary: str) -> dict:
            print(f"\n[CONFIRM] {tool_name}: {summary}\n", flush=True)
            if gemini and gemini.connected:
                await gemini.inject_text(
                    f"I need your approval. About to run: {tool_name}. {summary}. Yes or no?",
                    turn_complete=True,
                )
            return await gemini.wait_for_confirmation(action_id, timeout=60.0)

        mcp.set_confirm_callback(_confirm_cb)

    await gemini.connect()
    log.info("Gemini connected.")

    pumps = [
        asyncio.create_task(_mic_pump(), name="mic_pump"),
        asyncio.create_task(_speaker_pump(), name="speaker_pump"),
    ]

    log.info("Opening audio streams (mic %d Hz mono, speakers %d Hz mono).",
             INPUT_RATE, OUTPUT_RATE)
    print("\n" + "=" * 60)
    print("  Aria is listening. Speak normally. Ctrl+C to exit.")
    print("  (Use headphones to avoid the speaker feeding back into the mic.)")
    print("=" * 60 + "\n", flush=True)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Not supported on Windows; irrelevant on macOS/Linux
            pass

    try:
        with sd.InputStream(
            samplerate=INPUT_RATE,
            blocksize=INPUT_BLOCK,
            dtype="int16",
            channels=1,
            callback=_mic_callback,
        ), sd.OutputStream(
            samplerate=OUTPUT_RATE,
            blocksize=OUTPUT_BLOCK,
            dtype="int16",
            channels=1,
            callback=_speaker_callback,
        ):
            await stop_event.wait()
    finally:
        log.info("Shutting down...")
        for t in pumps:
            t.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        if gemini:
            try:
                await gemini.close()
            except Exception:
                log.exception("Error closing Gemini session")
        try:
            await cursor_bridge.stop()
        except Exception:
            log.exception("Error stopping Cursor bridge")
        if mcp:
            try:
                await mcp.stop_all()
            except Exception:
                log.exception("Error stopping MCP servers")
        log.info("Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
