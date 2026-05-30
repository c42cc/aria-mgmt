"""Python side of the Node.js subprocess bridge to @cursor/sdk.

Protocol contract (matches cursor_wrapper/index.js):

  Outbound (Python -> Node, on stdin):
    {"request_id": "<uuid>", "action": "...", ...}

  Inbound (Node -> Python, on stdout, one JSON per line):
    {"request_id": "<uuid>",   "type": "response", ...}    -> resolves _pending[rid]
    {"request_id": "<uuid>",   "type": "error",    "error": "..."} -> resolves _pending[rid] with error
    {"request_id": null,       "type": "event",    "session_id": "...", "event": "...", ...} -> per-session queue

A single reader task demultiplexes:
  - request_id present  -> route to the matching pending Future
  - request_id null + type=event -> route to the per-session_id event queue

Multiple concurrent build consumers each call `read_events(session_id)` and
receive only the events for their session. The shared-stdout race in the old
implementation is structurally impossible: responses and events live on
disjoint routing paths inside the same stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, AsyncIterator

from .config import config

log = logging.getLogger(__name__)


class CursorBridgeError(RuntimeError):
    """The Node-side bridge returned an explicit error response."""


class CursorBridge:
    """Manages the Node.js child process that wraps @cursor/sdk."""

    def __init__(self):
        self._process: asyncio.subprocess.Process | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._session_queues: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._closed = False

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """Start the Node.js cursor_wrapper subprocess. Idempotent."""
        if self.alive:
            return
        wrapper_path = os.path.join(config.cursor_wrapper_dir, "index.js")
        env = os.environ.copy()
        if config.cursor_api_key:
            env["CURSOR_API_KEY"] = config.cursor_api_key

        self._process = await asyncio.create_subprocess_exec(
            "node", wrapper_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=config.cursor_wrapper_dir,
            env=env,
        )
        self._closed = False
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        log.info("Cursor bridge subprocess started (pid=%s)", self._process.pid)

    async def _read_loop(self) -> None:
        """Single reader: demux responses to _pending, events to per-session queues."""
        assert self._process and self._process.stdout
        try:
            while True:
                raw = await self._process.stdout.readline()
                if not raw:
                    break
                try:
                    msg = json.loads(raw.decode())
                except json.JSONDecodeError:
                    log.warning("Non-JSON line from cursor bridge: %r", raw[:200])
                    continue

                rid = msg.get("request_id")
                mtype = msg.get("type", "")

                if rid is None or mtype == "event":
                    session_id = msg.get("session_id", "")
                    queue = self._session_queues.get(session_id)
                    if queue is not None:
                        await queue.put(msg)
                    else:
                        log.debug("Cursor event for unsubscribed session %s: %s", session_id, msg.get("event"))
                    continue

                fut = self._pending.pop(rid, None)
                if fut is None:
                    log.warning("Cursor bridge response for unknown request_id=%s", rid)
                    continue

                if mtype == "error":
                    fut.set_exception(CursorBridgeError(msg.get("error", "unknown error")))
                else:
                    fut.set_result(msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Cursor bridge read loop crashed")
        finally:
            for rid, fut in list(self._pending.items()):
                if not fut.done():
                    fut.set_exception(CursorBridgeError("Cursor bridge stdout closed"))
            self._pending.clear()
            # Signal all event consumers to terminate
            for queue in list(self._session_queues.values()):
                queue.put_nowait(None)
            self._closed = True

    async def _stderr_loop(self) -> None:
        """Drain Node's stderr so it never blocks. Log at debug."""
        assert self._process and self._process.stderr
        try:
            while True:
                raw = await self._process.stderr.readline()
                if not raw:
                    break
                text = raw.decode(errors="replace").rstrip()
                if text:
                    log.debug("cursor_wrapper stderr: %s", text)
        except asyncio.CancelledError:
            pass

    async def _send(self, command: dict, timeout: float = 30.0) -> dict[str, Any]:
        """Send a command; return the matching response (demuxed by request_id)."""
        if not self.alive or not self._process or not self._process.stdin:
            raise CursorBridgeError("Cursor bridge not started or already terminated")

        rid = str(uuid.uuid4())
        command = {**command, "request_id": rid}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut

        try:
            async with self._write_lock:
                self._process.stdin.write((json.dumps(command) + "\n").encode())
                await self._process.stdin.drain()
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise CursorBridgeError(
                f"Cursor bridge timeout after {timeout}s on action={command.get('action')!r}"
            )

    async def ping(self) -> dict[str, Any]:
        """Round-trip ping to verify the bridge protocol is healthy."""
        return await self._send({"action": "ping"}, timeout=5)

    async def create_session(
        self, project_path: str, instruction: str, model: str | None = None
    ) -> str:
        """Create a new Cursor agent session. Returns session_id and registers event queue.

        Also writes the new session into the unified `cursor_registry` so
        Aria's tools, the narrator, and the JSONL tailer see this SDK
        agent on the same footing as one of the user's IDE-opened windows.
        """
        resp = await self._send({
            "action": "create",
            "project_path": project_path,
            "instruction": instruction,
            "model": model or config.cursor_model,
        }, timeout=120)
        session_id = resp.get("session_id", "")
        if session_id:
            self._session_queues.setdefault(session_id, asyncio.Queue())
            try:
                from .cursor_registry import cursor_registry
                await cursor_registry.register_from_sdk(
                    session_id=session_id,
                    workspace_root=project_path,
                    instruction=instruction,
                )
            except Exception:
                log.exception(
                    "cursor_registry.register_from_sdk raised for session %s "
                    "(continuing — bridge state is unaffected)",
                    session_id,
                )
        return session_id

    async def send_message(self, session_id: str, message: str) -> dict[str, Any]:
        """Send a message to a running Cursor session."""
        return await self._send({
            "action": "send",
            "session_id": session_id,
            "message": message,
        })

    async def cancel_session(self, session_id: str) -> dict[str, Any]:
        """Cancel a running Cursor session."""
        return await self._send({
            "action": "cancel",
            "session_id": session_id,
        })

    async def read_events(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield events for a specific session. Returns when the bridge closes."""
        queue = self._session_queues.setdefault(session_id, asyncio.Queue())
        while True:
            msg = await queue.get()
            if msg is None:
                return
            yield msg

    def close_session(self, session_id: str) -> None:
        """Stop routing events for this session (drains queue, removes registration)."""
        queue = self._session_queues.pop(session_id, None)
        if queue is not None:
            queue.put_nowait(None)

    async def stop(self) -> None:
        """Terminate the subprocess and shut down reader tasks."""
        self._closed = True
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
            log.info("Cursor bridge subprocess stopped")
