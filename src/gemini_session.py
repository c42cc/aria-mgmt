"""Gemini 3.1 Live WebSocket session with bidirectional audio and function calling."""

from __future__ import annotations

import asyncio
import collections
import json
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
        name="rollback_prompt",
        description="Restore a prompt template to a previous version. Use when the user wants to undo a prompt edit. Call prompt_versions first to see available versions.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "name": types.Schema(type="STRING", description="Template name to rollback."),
                "version": types.Schema(type="INTEGER", description="Version number to restore."),
            },
            required=["name", "version"],
        ),
    ),
    types.FunctionDeclaration(
        name="prompt_versions",
        description="List all saved versions of a prompt template. Shows version numbers, when they were created, and how they originated (user edit, rollback, etc). Use before rollback_prompt.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "name": types.Schema(type="STRING", description="Template name to show history for."),
            },
            required=["name"],
        ),
    ),
    types.FunctionDeclaration(
        name="reload_prompts",
        description="Clear the prompt cache and reconnect your session so changes to your system prompt take effect immediately. Call after editing gemini_system. For other prompts, changes take effect on next use automatically.",
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="get_focused_app",
        description="Get the name and bundle ID of the frontmost Mac application. Use to check what app is currently focused before pasting.",
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="focus_app",
        description="Bring a Mac application to the front. Use when the user wants to paste into a specific app that isn't currently focused.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "app_name": types.Schema(type="STRING", description="Application name (e.g. 'Cursor', 'Notes', 'TextEdit', 'Slack')."),
            },
            required=["app_name"],
        ),
    ),
    types.FunctionDeclaration(
        name="dictate_into_focused_app",
        description=(
            "Type text into the frontmost Mac application by copying to clipboard and pasting. "
            "Use when the user says 'put this in', 'type into', 'paste into', or 'dictate into' an app. "
            "Call focus_app first if the target app is not already focused."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "text": types.Schema(type="STRING", description="The text to paste into the focused application."),
            },
            required=["text"],
        ),
    ),
    types.FunctionDeclaration(
        name="list_cursor_windows",
        description=(
            "List the titles of every Cursor IDE window currently open on the Mac. "
            "Use when the user asks what windows are open, or before targeting a specific window. "
            "Each entry tells you whether its title matches a registered project name."
        ),
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="read_cursor_window",
        description=(
            "Read the most recent transcript turns for a Cursor window/project. "
            "Use to catch up on what an external Cursor agent is doing or has done — what it said, "
            "what tools it called, and what plan files were just written. "
            "Project may be a registered short name or an absolute path."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "project": types.Schema(type="STRING", description="Registered project name (preferred) or absolute project cwd."),
                "n_turns": types.Schema(type="INTEGER", description="How many recent turns to return (default 5, max 25)."),
            },
            required=["project"],
        ),
    ),
    types.FunctionDeclaration(
        name="list_cursor_plans",
        description=(
            "List plan files written under ~/.cursor/plans/ in the last N minutes. "
            "Use when the user asks 'is there a new plan?' or 'show me recent plans'."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "max_age_minutes": types.Schema(type="INTEGER", description="Plan recency window in minutes (default 60)."),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="focus_cursor_window",
        description=(
            "Bring a specific Cursor IDE window to the front by matching its title against the project "
            "name. Must be called before any send_to_cursor_chat / keystroke_to_cursor_window / "
            "screenshot_cursor_window (those tools focus implicitly, but call this if you want to verify "
            "the right window was found first)."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "project": types.Schema(type="STRING", description="Registered project name (preferred) or substring matching the window title."),
            },
            required=["project"],
        ),
    ),
    types.FunctionDeclaration(
        name="send_to_cursor_chat",
        description=(
            "Type a message into a specific Cursor window and send it. Aria's primary way to "
            "relay Corbin's spoken instructions into a Cursor agent. With new_agent=True "
            "(default) opens a fresh agent composer (Cmd+I) and starts a new agent task; "
            "new_agent=False opens the existing chat sidebar (Cmd+L) for a follow-up. "
            "Returns ok=True if the keystrokes fired against the focused Cursor window — "
            "this is NOT proof the agent received them. To confirm the send actually landed, "
            "wait 8-15 seconds and call read_cursor_window: if you see a new user turn or "
            "an in-progress assistant turn, it worked. Only retry the send if read_cursor_window "
            "shows no new turn after the wait. Always translate Corbin's casual voice intent "
            "into a precise, well-formed prompt before sending."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "project": types.Schema(type="STRING", description="Registered project name or substring matching the window title."),
                "message": types.Schema(type="STRING", description="The refined, well-formed message to type into the chat input."),
                "new_agent": types.Schema(type="BOOLEAN", description="True (default) starts a NEW agent task; False sends into the existing chat."),
            },
            required=["project", "message"],
        ),
    ),
    types.FunctionDeclaration(
        name="keystroke_to_cursor_window",
        description=(
            "Escape hatch: send arbitrary keystrokes to a Cursor window after focusing it. "
            "Use for shortcuts (Cmd+P, Cmd+Shift+L, Esc, etc.) when the dedicated tools don't fit. "
            "For typing a chat message prefer send_to_cursor_chat."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "project": types.Schema(type="STRING", description="Registered project name or window-title substring."),
                "keys": types.Schema(type="STRING", description="Literal keystrokes (System Events keystroke argument)."),
                "modifiers": types.Schema(type="STRING", description="Comma-separated subset of {command, control, option, shift}."),
            },
            required=["project", "keys"],
        ),
    ),
    types.FunctionDeclaration(
        name="screenshot_cursor_window",
        description=(
            "Take a screenshot of a Cursor window for visual context. "
            "Use when you need to see the current state of a window — plan-mode UI, an error dialog, "
            "or unexpected layout. Returns the saved PNG path."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "project": types.Schema(type="STRING", description="Registered project name or window-title substring."),
                "save_path": types.Schema(type="STRING", description="Optional absolute path for the PNG. Defaults to data/screenshots/."),
            },
            required=["project"],
        ),
    ),
    types.FunctionDeclaration(
        name="approve_cursor_plan",
        description=(
            "Approve and proceed on a Cursor plan-mode plan by typing 'Approve and proceed.' into "
            "the target window's chat. Use after read_cursor_window shows a plan that Corbin verbally OK'd."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "project": types.Schema(type="STRING", description="Registered project name or window-title substring."),
                "note": types.Schema(type="STRING", description="Optional extra context appended to the approval message."),
            },
            required=["project"],
        ),
    ),
    types.FunctionDeclaration(
        name="reject_cursor_plan",
        description=(
            "Reject a Cursor plan-mode plan by typing 'Stop. Do not proceed with this plan.' Use when "
            "Corbin verbally vetoes a plan and you need to halt the agent."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "project": types.Schema(type="STRING", description="Registered project name or window-title substring."),
                "reason": types.Schema(type="STRING", description="Optional reason to communicate to the agent."),
            },
            required=["project"],
        ),
    ),
]

TranscriptEntry = collections.namedtuple("TranscriptEntry", ["role", "text", "ts"])


class GeminiSession:
    """Manages a Gemini Live session with audio streaming and tool dispatch."""

    def __init__(
        self,
        tool_handler: Callable[..., Coroutine] | None = None,
        transcript_callback: Callable[[str, str], Coroutine] | None = None,
        orphan_callback: Callable[[str, str, str], Coroutine] | None = None,
    ):
        """
        transcript_callback(role, text) is invoked once per *completed* turn
        with role in {"user", "aria"} and the full transcribed text. Used
        by bot.py to record the turn into the shared ConversationBuffer
        and mirror it to the voice-channel text chat.

        orphan_callback(tool_name, fc_id, result_text) is invoked when a
        tool dispatch finishes but the session has already closed so the
        result cannot be sent back to Gemini. This is the loud-failure
        signal for L1 — a side-effect happened (MCP write/send) but the
        model never heard about it. The bot routes this to #ucs-alerts.
        """
        self.tool_handler = tool_handler
        self.transcript_callback = transcript_callback
        self.orphan_callback = orphan_callback
        self._client: genai.Client | None = None
        self._session: Any = None
        self._session_ctx: Any = None
        self._audio_out_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self._receive_task: asyncio.Task | None = None
        self._connected = False

        self._transcript_buffer: collections.deque[TranscriptEntry] = collections.deque(maxlen=100)

        # Per-turn accumulators; flushed to the buffer and the callback
        # when Gemini signals turn_complete (or the session is closing).
        self._user_turn_acc: str = ""
        self._aria_turn_acc: str = ""

        self._pending_confirmations: dict[str, asyncio.Event] = {}
        self._confirmation_results: dict[str, dict] = {}

        self._lifecycle_lock = asyncio.Lock()
        self._served_fc_ids: set[str] = set()

        # Track in-flight dispatch tasks so we can await them on close.
        # Without this, _do_close cancels the receive loop but a side-
        # effecting tool can still be running unobserved, lose its
        # response, and leave the model unaware of what happened. The
        # close path now awaits these with a bounded timeout (L1 fix).
        self._dispatch_tasks: set[asyncio.Task] = set()

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Establish Gemini Live WebSocket connection.

        Idempotent: returns immediately if already connected with a live
        receive task. All callers are safe to call without external guards.
        """
        async with self._lifecycle_lock:
            if self._connected and self._receive_task and not self._receive_task.done():
                log.debug("connect() called but already connected — skipping")
                return
            await self._do_connect()
            self._receive_task = asyncio.create_task(self._receive_loop())
        log.info("Gemini Live session connected (model=%s)", config.gemini_model)

    async def _do_connect(self) -> None:
        """Create a new Gemini Live session. Caller must hold _lifecycle_lock."""
        if not config.google_api_key:
            raise RuntimeError("GEMINI_API_KEY not set")

        self._user_turn_acc = ""
        self._aria_turn_acc = ""

        self._client = genai.Client(api_key=config.google_api_key)

        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Kore",
                    ),
                ),
            ),
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

    def get_recent_transcript(self, max_turns: int = 3) -> list[dict[str, str]]:
        """Structured recent transcript for session record capture."""
        return [
            {"role": e.role, "text": e.text}
            for e in list(self._transcript_buffer)[-max_turns:]
            if e.text
        ]

    async def _flush_turn_accumulators(self) -> None:
        """Emit accumulated per-turn transcripts to the callback, then reset.

        Called on every `turn_complete` (and `interrupted`) signal from
        Gemini. The callback is invoked at most once per role per turn
        with the full transcript text. If the callback raises, we log
        and continue — a misbehaving downstream must not break voice.
        """
        user_text = self._user_turn_acc.strip()
        aria_text = self._aria_turn_acc.strip()
        self._user_turn_acc = ""
        self._aria_turn_acc = ""

        if not self.transcript_callback:
            return

        if user_text:
            try:
                await self.transcript_callback("user", user_text)
            except Exception:
                log.exception("transcript_callback failed for user turn")
        if aria_text:
            try:
                await self.transcript_callback("aria", aria_text)
            except Exception:
                log.exception("transcript_callback failed for aria turn")

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
                                    self._aria_turn_acc += part.text

                        if sc.input_transcription and sc.input_transcription.text:
                            self._transcript_buffer.append(
                                TranscriptEntry("user", sc.input_transcription.text, time.time())
                            )
                            self._user_turn_acc += sc.input_transcription.text

                        if sc.output_transcription and sc.output_transcription.text:
                            self._transcript_buffer.append(
                                TranscriptEntry("assistant", sc.output_transcription.text, time.time())
                            )
                            self._aria_turn_acc += sc.output_transcription.text

                        if getattr(sc, "turn_complete", False) or getattr(sc, "interrupted", False):
                            await self._flush_turn_accumulators()

                    elif msg.tool_call:
                        seen_in_turn: set[str] = set()
                        for fc in msg.tool_call.function_calls:
                            if fc.id in self._served_fc_ids:
                                log.info("Skipping already-served fc.id=%s (%s)", fc.id, fc.name)
                                continue

                            dedup_key = f"{fc.name}:{json.dumps(dict(fc.args) if fc.args else {}, sort_keys=True)}"
                            if dedup_key in seen_in_turn:
                                log.info("Skipping duplicate in-turn call: %s", fc.name)
                                continue
                            seen_in_turn.add(dedup_key)
                            self._served_fc_ids.add(fc.id)

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
                                task = asyncio.create_task(self._dispatch_tool_call(fc))
                                self._dispatch_tasks.add(task)
                                task.add_done_callback(self._dispatch_tasks.discard)

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
                if self._session_ctx:
                    try:
                        await self._session_ctx.__aexit__(None, None, None)
                    except Exception:
                        pass
                    self._session_ctx = None

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

                try:
                    await self._do_connect()
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

    async def _dispatch_tool_call(self, fc: Any) -> None:
        """Run a tool handler and send the response back to Gemini.

        Runs as a separate task so the receive loop stays free to process
        confirm_action while tier-I/X tools await user approval. Tracked in
        `self._dispatch_tasks` so close() can await in-flight dispatches.
        """
        try:
            result = await self.tool_handler(
                fc.name, dict(fc.args) if fc.args else {}
            )
        except Exception as e:
            log.exception("Tool handler error for %s", fc.name)
            result = f'{{"error": "{e}"}}'

        if not self._session or not self._connected:
            # The session went away while the tool was running. The side
            # effect may have already happened (Gmail send, calendar create,
            # filesystem write). Surface this loudly — Gemini will never see
            # the result, but the operator must.
            log.error(
                "ORPHAN TOOL RESULT: %s (id=%s) completed but session closed "
                "before response could be sent. Result preview: %s",
                fc.name, getattr(fc, "id", "?"), str(result)[:300],
            )
            if self.orphan_callback:
                try:
                    await self.orphan_callback(fc.name, getattr(fc, "id", ""), str(result))
                except Exception:
                    log.exception("orphan_callback failed for %s", fc.name)
            return

        try:
            await self._session.send_tool_response(
                function_responses=types.FunctionResponse(
                    name=fc.name,
                    response={"result": result},
                    id=fc.id,
                )
            )
        except Exception:
            log.exception("Failed to send tool response for %s", fc.name)
            if self.orphan_callback:
                try:
                    await self.orphan_callback(fc.name, getattr(fc, "id", ""), str(result))
                except Exception:
                    log.exception("orphan_callback failed (send-tool-response branch) for %s", fc.name)

    async def reconnect(self) -> None:
        """Gracefully close and reopen the session with fresh prompts.

        Preserves recent transcript context across the reconnect so
        the conversation feels continuous.
        """
        async with self._lifecycle_lock:
            from .prompts import clear_cache
            clear_cache()
            self._served_fc_ids.clear()
            context = self.get_transcript_context(max_turns=5)
            await self._do_close()
            await self._do_connect()
            self._receive_task = asyncio.create_task(self._receive_loop())
        if context:
            await self.inject_text(
                f"Session resumed after prompt reload. Recent context:\n{context}",
                turn_complete=False,
            )
        log.info("Gemini session reconnected after prompt reload")

    async def close(self) -> None:
        """Close the Gemini session. Idempotent."""
        async with self._lifecycle_lock:
            await self._do_close()

    async def _do_close(self) -> None:
        """Internal close. Caller must hold _lifecycle_lock."""
        if not self._connected and not self._session and not self._session_ctx:
            return
        self._connected = False
        try:
            await self._flush_turn_accumulators()
        except Exception:
            log.exception("Error flushing turn accumulators on close")

        # Wait briefly for in-flight tool dispatches to finish so their
        # results can be sent back to Gemini before the session closes.
        # Bounded by 5s — beyond that we accept orphan-tool-result loss
        # and surface it via orphan_callback. Without this wait, every
        # in-flight tool at close time becomes a silent loss (L1).
        in_flight = {t for t in self._dispatch_tasks if not t.done()}
        if in_flight:
            log.info(
                "Waiting up to 5s for %d in-flight tool dispatch(es) before close",
                len(in_flight),
            )
            done, pending = await asyncio.wait(in_flight, timeout=5.0)
            for t in pending:
                log.error(
                    "Tool dispatch did not finish within close window — cancelling. "
                    "Side effect may have completed without a model-visible response."
                )
                t.cancel()

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
