"""Gemini 3.1 Live WebSocket session with bidirectional audio and function calling."""

from __future__ import annotations

import logging
from typing import Any, Callable

from .config import config
from .prompts import load_template

log = logging.getLogger(__name__)


TOOL_DECLARATIONS = [
    {
        "name": "plan_with_claude",
        "description": "Send a planning request to Claude Opus 4.6 for analysis, planning, architecture, or debugging strategy.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt_template": {
                    "type": "string",
                    "description": "Name of template in prompts/ (e.g. 'refactor', 'architecture', 'bug-analysis'). Defaults to 'planning'.",
                },
                "context": {
                    "type": "string",
                    "description": "Assembled context: user's request, file contents, prior plan, feedback.",
                },
                "session_key": {
                    "type": "string",
                    "description": "Discord thread ID (groups related planning calls).",
                },
            },
            "required": ["context", "session_key"],
        },
    },
    {
        "name": "build_with_cursor",
        "description": "Start a Cursor agent to build/edit code on a project using an approved plan.",
        "parameters": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project name from projects/registry.md.",
                },
                "instruction": {
                    "type": "string",
                    "description": "Approved plan + implementation instructions for Cursor.",
                },
                "background": {
                    "type": "boolean",
                    "description": "Run in background (default true). Returns session_id immediately.",
                },
            },
            "required": ["project", "instruction"],
        },
    },
    {
        "name": "query_cursor",
        "description": "Send a message to a running Cursor build session (e.g. answering a question it asked).",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["session_id", "message"],
        },
    },
    {
        "name": "cursor_status",
        "description": "Get status of all running/waiting Cursor build sessions.",
        "parameters": {"type": "object", "properties": {}},
    },
]


class GeminiSession:
    """Manages a Gemini Live session with audio streaming and tool dispatch."""

    def __init__(self, tool_handler: Callable[[str, dict], Any] | None = None):
        self.tool_handler = tool_handler
        self._session = None

    async def connect(self) -> None:
        """Establish Gemini Live WebSocket connection."""
        # TODO: Initialize google-genai client with config.google_api_key
        # TODO: Open Live session with config.gemini_model
        # TODO: Set system prompt from load_template("gemini_system")
        # TODO: Register TOOL_DECLARATIONS
        log.info("Gemini session connect — not yet implemented")

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Send PCM audio chunk to Gemini."""
        # TODO: Stream audio to the Live session
        pass

    async def receive_audio(self) -> bytes | None:
        """Receive PCM audio chunk from Gemini, if available."""
        # TODO: Read audio from the Live session
        return None

    async def handle_tool_call(self, name: str, args: dict) -> str:
        """Dispatch a Gemini function call to the appropriate tool handler."""
        if self.tool_handler:
            return await self.tool_handler(name, args)
        return '{"error": "no tool handler configured"}'

    async def close(self) -> None:
        """Close the Gemini session."""
        if self._session:
            # TODO: Close the session
            pass
        log.info("Gemini session closed")
