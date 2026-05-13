"""Gemini 3.1 Live WebSocket session with bidirectional audio and function calling."""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Any, Callable, Coroutine

from google import genai
from google.genai import types

from .config import config
from .prompts import load_template

log = logging.getLogger(__name__)


TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="plan_with_claude",
        description="Send a planning request to Claude Opus 4.6 for analysis, planning, architecture, or debugging strategy.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "prompt_template": types.Schema(type="STRING", description="Name of template in prompts/ (e.g. 'refactor', 'architecture', 'bug-analysis'). Defaults to 'planning'."),
                "context": types.Schema(type="STRING", description="Assembled context: user's request, file contents, prior plan, feedback."),
                "session_key": types.Schema(type="STRING", description="Discord thread ID (groups related planning calls)."),
            },
            required=["context", "session_key"],
        ),
    ),
    types.FunctionDeclaration(
        name="build_with_cursor",
        description="Start a Cursor agent to build/edit code on a project using an approved plan.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "project": types.Schema(type="STRING", description="Project name from projects/registry.md."),
                "instruction": types.Schema(type="STRING", description="Approved plan + implementation instructions for Cursor."),
                "background": types.Schema(type="BOOLEAN", description="Run in background (default true). Returns session_id immediately."),
            },
            required=["project", "instruction"],
        ),
    ),
    types.FunctionDeclaration(
        name="query_cursor",
        description="Send a message to a running Cursor build session (e.g. answering a question it asked).",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "session_id": types.Schema(type="STRING"),
                "message": types.Schema(type="STRING"),
            },
            required=["session_id", "message"],
        ),
    ),
    types.FunctionDeclaration(
        name="cursor_status",
        description="Get status of all running/waiting Cursor build sessions.",
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="do_with_claude",
        description="Execute a complex multi-step task using Claude Opus 4.6 with tool access. Use for email, calendar, file management, research, or any non-coding task that requires reasoning and actions.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "task": types.Schema(type="STRING", description="Natural language description of what to do."),
                "session_key": types.Schema(type="STRING", description="Discord thread ID."),
            },
            required=["task", "session_key"],
        ),
    ),
    types.FunctionDeclaration(
        name="remember",
        description="Store a durable fact in long-term memory (e.g. preferences, contacts, project details).",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "text": types.Schema(type="STRING", description="The fact to remember."),
            },
            required=["text"],
        ),
    ),
    types.FunctionDeclaration(
        name="recall",
        description="Search long-term memory for relevant facts.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "query": types.Schema(type="STRING", description="What to search for."),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="confirm_action",
        description="User has approved or rejected a pending action that required confirmation. Call this after the user responds to a confirmation prompt.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "action_id": types.Schema(type="STRING", description="The ID of the pending action."),
                "approved": types.Schema(type="BOOLEAN", description="Whether the user approved."),
                "modifications": types.Schema(type="STRING", description="Optional changes the user requested before approving."),
            },
            required=["action_id", "approved"],
        ),
    ),
    types.FunctionDeclaration(
        name="cancel_current_task",
        description="Cancel the currently running task (build or multi-step action). Use when the user says stop, abort, cancel, or nevermind.",
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="quick_email_check",
        description="Fast read-only check of unread mail. Use for 'do I have any new emails' style questions; bypasses the full Claude loop.",
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="quick_calendar",
        description="Fast read-only check of upcoming calendar events. Use for 'what's on my calendar' style questions.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "days_ahead": types.Schema(type="INTEGER", description="Window in days (default 1 = today + tomorrow)."),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="list_prompts",
        description="List all available prompt templates that define your behavior and tool personas. Use when the user asks what prompts you have or wants to see the prompt catalog.",
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="show_prompt",
        description="Read a prompt template and post the full text to the text channel. Use when the user asks to see a specific prompt. Speak a brief summary; the full text goes to the channel.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "name": types.Schema(type="STRING", description="Template name (e.g. 'gemini_system', 'planning', 'implementation')."),
            },
            required=["name"],
        ),
    ),
    types.FunctionDeclaration(
        name="edit_prompt",
        description="Edit a prompt template based on a natural-language instruction. Reads the current prompt, applies the change via Claude, saves it, and posts the new version to the text channel. Use when the user asks to change, update, or modify a prompt.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "name": types.Schema(type="STRING", description="Template name to edit."),
                "instruction": types.Schema(type="STRING", description="Natural-language description of the desired change."),
            },
            required=["name", "instruction"],
        ),
    ),
    types.FunctionDeclaration(
        name="reload_prompts",
        description="Clear the prompt cache and reconnect your session so changes to your system prompt take effect immediately. Call after editing gemini_system. For other prompts, changes take effect on next use automatically.",
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
]

TranscriptEntry = collections.namedtuple("TranscriptEntry", ["role", "text", "ts"])


class GeminiSession:
    """Manages a Gemini Live session with audio streaming and tool dispatch."""

    def __init__(self, tool_handler: Callable[..., Coroutine] | None = None):
        self.tool_handler = tool_handler
        self._client: genai.Client | None = None
        self._session: Any = None
        self._session_ctx: Any = None
        self._audio_out_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self._receive_task: asyncio.Task | None = None
        self._connected = False

        self._transcript_buffer: collections.deque[TranscriptEntry] = collections.deque(maxlen=100)

        self._pending_confirmations: dict[str, asyncio.Event] = {}
        self._confirmation_results: dict[str, dict] = {}

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Establish Gemini Live WebSocket connection."""
        if not config.google_api_key:
            raise RuntimeError("GEMINI_API_KEY not set")

        self._client = genai.Client(api_key=config.google_api_key)

        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            tools=TOOL_DECLARATIONS,
            system_instruction=types.Content(
                parts=[types.Part(text=load_template("gemini_system"))]
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )

        self._session_ctx = self._client.aio.live.connect(
            model=config.gemini_model, config=live_config
        )
        self._session = await self._session_ctx.__aenter__()
        self._connected = True
        self._receive_task = asyncio.create_task(self._receive_loop())
        log.info("Gemini Live session connected (model=%s)", config.gemini_model)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Send PCM audio chunk (16kHz mono int16) to Gemini."""
        if not self._session or not self._connected:
            return
        try:
            await self._session.send_realtime_input(
                audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
            )
        except Exception:
            log.exception("Failed to send audio to Gemini")
            self._connected = False

    async def inject_text(self, text: str, turn_complete: bool = True) -> None:
        """Inject text into the Gemini session context.

        turn_complete=True: Gemini responds immediately (use for questions, confirmations).
        turn_complete=False: Added to context silently (use for session resume, background info).
        """
        if not self._session or not self._connected:
            return
        try:
            await self._session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=text)]),
                turn_complete=turn_complete,
            )
        except Exception:
            log.exception("Failed to inject text into Gemini session")

    async def get_audio(self) -> bytes:
        """Get the next audio chunk from the output queue."""
        return await self._audio_out_queue.get()

    def get_transcript_context(self, max_turns: int = 5) -> str:
        """Get recent transcript for session reconnect context."""
        recent = list(self._transcript_buffer)[-max_turns:]
        if not recent:
            return ""
        lines = [f"{e.role}: {e.text}" for e in recent if e.text]
        return "\n".join(lines)

    async def wait_for_confirmation(self, action_id: str, timeout: float = 60.0) -> dict:
        """Wait for a confirm_action tool call with the given action_id."""
        event = asyncio.Event()
        self._pending_confirmations[action_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return self._confirmation_results.pop(action_id, {"approved": False})
        except asyncio.TimeoutError:
            return {"approved": False, "timeout": True}
        finally:
            self._pending_confirmations.pop(action_id, None)

    async def _receive_loop(self) -> None:
        """Main receive loop: process audio, transcriptions, and tool calls from Gemini."""
        backoff = 1.0
        max_backoff = 30.0

        while True:
            try:
                if not self._session or not self._connected:
                    break

                async for msg in self._session.receive():
                    if not self._connected:
                        return
                    backoff = 1.0

                    if msg.server_content:
                        sc = msg.server_content
                        if sc.model_turn:
                            for part in sc.model_turn.parts:
                                if part.inline_data and part.inline_data.data:
                                    try:
                                        self._audio_out_queue.put_nowait(part.inline_data.data)
                                    except asyncio.QueueFull:
                                        try:
                                            self._audio_out_queue.get_nowait()
                                        except asyncio.QueueEmpty:
                                            pass
                                        self._audio_out_queue.put_nowait(part.inline_data.data)

                                if part.text:
                                    self._transcript_buffer.append(
                                        TranscriptEntry("assistant", part.text, time.time())
                                    )

                        if sc.input_transcription and sc.input_transcription.text:
                            self._transcript_buffer.append(
                                TranscriptEntry("user", sc.input_transcription.text, time.time())
                            )

                        if sc.output_transcription and sc.output_transcription.text:
                            self._transcript_buffer.append(
                                TranscriptEntry("assistant", sc.output_transcription.text, time.time())
                            )

                    elif msg.tool_call:
                        for fc in msg.tool_call.function_calls:
                            log.info("Gemini tool call: %s(%s)", fc.name, fc.id)

                            if fc.name == "confirm_action":
                                args = dict(fc.args) if fc.args else {}
                                action_id = args.get("action_id", "")
                                if action_id in self._pending_confirmations:
                                    self._confirmation_results[action_id] = {
                                        "approved": args.get("approved", False),
                                        "modifications": args.get("modifications"),
                                    }
                                    self._pending_confirmations[action_id].set()
                                await self._session.send_tool_response(
                                    function_responses=types.FunctionResponse(
                                        name=fc.name,
                                        response={"result": "confirmation recorded"},
                                        id=fc.id,
                                    )
                                )
                                continue

                            if self.tool_handler:
                                try:
                                    result = await self.tool_handler(
                                        fc.name, dict(fc.args) if fc.args else {}
                                    )
                                except Exception as e:
                                    log.exception("Tool handler error for %s", fc.name)
                                    result = f'{{"error": "{e}"}}'

                                await self._session.send_tool_response(
                                    function_responses=types.FunctionResponse(
                                        name=fc.name,
                                        response={"result": result},
                                        id=fc.id,
                                    )
                                )

            except asyncio.CancelledError:
                log.info("Gemini receive loop cancelled")
                self._connected = False
                return

            except Exception:
                log.exception("Gemini receive loop error — reconnecting in %.0fs", backoff)
                self._connected = False

                if self._session:
                    try:
                        await self._session.close()
                    except Exception:
                        pass
                    self._session = None

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

                try:
                    await self.connect()
                    context = self.get_transcript_context(max_turns=3)
                    if context:
                        await self.inject_text(
                            f"Session resumed. Recent context:\n{context}",
                            turn_complete=False,
                        )
                    log.info("Gemini session reconnected after error")
                except Exception:
                    log.exception("Gemini reconnect failed")
                    self._connected = False
                    return

    async def reconnect(self) -> None:
        """Gracefully close and reopen the session with fresh prompts.

        Preserves recent transcript context across the reconnect so
        the conversation feels continuous.
        """
        from .prompts import clear_cache
        clear_cache()
        context = self.get_transcript_context(max_turns=5)
        await self.close()
        await self.connect()
        if context:
            await self.inject_text(
                f"Session resumed after prompt reload. Recent context:\n{context}",
                turn_complete=False,
            )
        log.info("Gemini session reconnected after prompt reload")

    async def close(self) -> None:
        """Close the Gemini session."""
        self._connected = False
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._session_ctx:
            try:
                await self._session_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._session_ctx = None
            self._session = None
        elif self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        log.info("Gemini session closed")
