"""Python side of the Node.js subprocess bridge to @cursor/sdk."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator

from .config import config

log = logging.getLogger(__name__)


class CursorBridge:
    """Manages the Node.js child process that wraps @cursor/sdk."""

    def __init__(self):
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        """Start the Node.js cursor_wrapper subprocess."""
        wrapper_path = os.path.join(config.cursor_wrapper_dir, "index.js")
        self._process = await asyncio.create_subprocess_exec(
            "node", wrapper_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=config.cursor_wrapper_dir,
        )
        log.info("Cursor bridge subprocess started (pid=%s)", self._process.pid)

    async def create_session(
        self, project_path: str, instruction: str, model: str | None = None
    ) -> str:
        """Create a new Cursor agent session. Returns session_id."""
        resp = await self._send({
            "action": "create",
            "project_path": project_path,
            "instruction": instruction,
            "model": model or config.cursor_model,
        })
        return resp.get("session_id", "")

    async def send_message(self, session_id: str, message: str) -> dict[str, Any]:
        """Send a message to a running Cursor session."""
        return await self._send({
            "action": "send",
            "session_id": session_id,
            "message": message,
        })

    async def _send(self, command: dict) -> dict[str, Any]:
        """Send a JSON command to the subprocess and read one JSON response."""
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise RuntimeError("Cursor bridge not started")

        line = json.dumps(command) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()

        raw = await self._process.stdout.readline()
        if not raw:
            raise RuntimeError("Cursor bridge subprocess closed stdout")
        return json.loads(raw.decode())

    async def read_events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield events from the subprocess stdout stream."""
        if not self._process or not self._process.stdout:
            return
        while True:
            raw = await self._process.stdout.readline()
            if not raw:
                break
            try:
                yield json.loads(raw.decode())
            except json.JSONDecodeError:
                log.warning("Non-JSON line from cursor bridge: %s", raw)

    async def stop(self) -> None:
        """Terminate the subprocess."""
        if self._process:
            self._process.terminate()
            await self._process.wait()
            log.info("Cursor bridge subprocess stopped")
