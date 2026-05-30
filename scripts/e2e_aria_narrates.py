#!/usr/bin/env python3
"""End-to-end Cursor remote-pilot test where Aria narrates each step.

Next iteration of `scripts/e2e_remote_pilot.py`. Same harness — opens a fresh
Cursor IDE window, registers the project, sends a real plan into chat, watches
the transcript — but every step is also narrated into Discord as if Aria were
talking through what she's about to do.

By default the narration posts to the text channel only (raw Discord HTTP,
matches the pattern in `tests/deep_integration.py`). With `--with-voice`, each
line is also POSTed to the bot's localhost `/aria_say` endpoint, which calls
`gemini.inject_text(..., turn_complete=True)` so Aria *speaks* the line aloud
over the voice channel.

Two narration sources merge in the same text channel:

  1. Driver labels  ("I'm opening Cursor now. Submitting the plan...")
  2. Observer pages ("Cursor just told me: Plan constructed in aria_e2e_…")

The observer pages come from `CursorExternalObserver` hooked to the user-level
Cursor hooks forwarder, exactly the same way Aria sees the other Cursor
windows in production.

USAGE
  .venv/bin/python scripts/e2e_aria_narrates.py
  .venv/bin/python scripts/e2e_aria_narrates.py --with-voice
  .venv/bin/python scripts/e2e_aria_narrates.py --keep-project --skip-cursor-open

PRE-REQS
  - `DISCORD_APP_BOT_TOKEN` and `DISCORD_TEXT_CHANNEL_ID` set in .env.
  - For `--with-voice`: the bot is running (`make run`), you have joined the
    voice channel (so Aria auto-joined and `gemini.connected` is True), and
    the observer is listening on UCS_CURSOR_EVENT_HOST:UCS_CURSOR_EVENT_PORT
    (default 127.0.0.1:8731).

PORT BEHAVIOR
  - `--with-voice` ON: the bot owns the observer port, so this script does NOT
    start its own observer. Observer pages from the bot go to `#ucs-alerts`
    via the bot's normal pager; this script only narrates its own driver
    steps to `#ucs-text`. The two streams are visible side by side in Discord.
  - `--with-voice` OFF: the bot is assumed NOT running. This script starts its
    own observer on the configured port and narrates both driver steps and
    observer pages to `#ucs-text`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import aiohttp

from src import tools as tools_module
from src.config import config
from src.cursor_bridge import CursorBridge
from src.cursor_external import CursorExternalObserver
from src.cursor_registry import RegistryEvent, cursor_registry


# ---------------------------------------------------------------------------
# Narrator: posts each driver step to Discord text + optionally Aria voice
# ---------------------------------------------------------------------------

class Narrator:
    """Pipes the driver's per-step narration into Discord.

    Always posts to the text channel via the Discord HTTP API (no bot
    process required for this path — uses DISCORD_APP_BOT_TOKEN directly).
    When `with_voice=True`, also POSTs to the running bot's `/aria_say`
    endpoint so Gemini speaks the same line aloud.

    Failures are loud-but-non-fatal: a 4xx/5xx response is logged to stdout
    so the operator sees it immediately, but the test loop keeps running
    so a missing-voice configuration doesn't kill the harness.
    """

    DISCORD_API_BASE = "https://discord.com/api/v10"
    DEFAULT_VOICE_TIMEOUT_SEC = 8.0
    DEFAULT_TEXT_TIMEOUT_SEC = 8.0

    def __init__(
        self,
        *,
        bot_token: str,
        text_channel_id: str,
        with_voice: bool,
        aria_say_url: str,
    ) -> None:
        self._bot_token = bot_token
        self._text_channel_id = text_channel_id
        self._with_voice = with_voice
        self._aria_say_url = aria_say_url
        self._session: aiohttp.ClientSession | None = None
        self._step_counter = 0
        self._voice_503_warned = False

    async def __aenter__(self) -> "Narrator":
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def say(self, text: str, *, is_step: bool = True) -> None:
        """Narrate `text` to Discord text and (optionally) Aria's voice.

        `is_step=True` (default) bumps the step counter and prefixes the
        text-channel post with the step number for easier scanning.
        `is_step=False` is for asides like observer pages ("Cursor just
        told me ..."), printed without a step number.
        """
        if is_step:
            self._step_counter += 1
            text_label = f"**[step {self._step_counter}]** {text}"
            print(f"\n=== STEP {self._step_counter}: {text}", flush=True)
        else:
            text_label = text
            print(f"  >> {text}", flush=True)

        await self._post_to_text_channel(text_label)
        if self._with_voice:
            await self._post_to_aria_say(text)

    async def _post_to_text_channel(self, content: str) -> None:
        assert self._session is not None
        url = f"{self.DISCORD_API_BASE}/channels/{self._text_channel_id}/messages"
        headers = {
            "Authorization": f"Bot {self._bot_token}",
            "Content-Type": "application/json",
        }
        body = {"content": content[:2000]}
        try:
            async with self._session.post(
                url,
                headers=headers,
                json=body,
                timeout=aiohttp.ClientTimeout(total=self.DEFAULT_TEXT_TIMEOUT_SEC),
            ) as resp:
                if resp.status >= 400:
                    err = await resp.text()
                    print(
                        f"  WARN: Discord text post {resp.status}: {err[:200]}",
                        flush=True,
                    )
        except Exception as exc:
            print(f"  WARN: Discord text post raised: {exc}", flush=True)

    async def _post_to_aria_say(self, text: str) -> None:
        assert self._session is not None
        try:
            async with self._session.post(
                self._aria_say_url,
                json={"text": text, "turn_complete": True},
                timeout=aiohttp.ClientTimeout(total=self.DEFAULT_VOICE_TIMEOUT_SEC),
            ) as resp:
                if resp.status == 503:
                    if not self._voice_503_warned:
                        body = await resp.text()
                        print(
                            f"  WARN: /aria_say 503 ({body[:200]}). "
                            "Is the bot running and are you in the voice channel? "
                            "(Further 503s will be silently swallowed.)",
                            flush=True,
                        )
                        self._voice_503_warned = True
                elif resp.status >= 400:
                    body = await resp.text()
                    print(
                        f"  WARN: /aria_say {resp.status}: {body[:200]}",
                        flush=True,
                    )
        except aiohttp.ClientConnectorError:
            if not self._voice_503_warned:
                print(
                    f"  WARN: /aria_say unreachable at {self._aria_say_url}. "
                    "Bot is not running. Voice narration disabled for the rest of "
                    "this run.",
                    flush=True,
                )
                self._voice_503_warned = True
        except Exception as exc:
            print(f"  WARN: /aria_say raised: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Project skeleton + cleanup (same shape as e2e_remote_pilot.py)
# ---------------------------------------------------------------------------

def make_skeleton_project(root: str) -> None:
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "README.md"), "w") as f:
        f.write(
            "# Aria E2E Test Project\n\n"
            "Temporary project. Aria is going to ask Cursor to extend it.\n"
        )
    with open(os.path.join(root, "calculator.py"), "w") as f:
        f.write(
            '"""Tiny calculator skeleton — Aria is going to extend this."""\n\n'
            "from __future__ import annotations\n\n\n"
            "def add(a: float, b: float) -> float:\n"
            "    return a + b\n"
        )


def cleanup_project(root: str) -> None:
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------

DEFAULT_INSTRUCTION = (
    "Please extend calculator.py with the following:\n"
    "1. A subtract(a, b) function that returns a - b.\n"
    "2. A multiply(a, b) function that returns a * b.\n"
    "3. Type hints and one-line docstrings on every function.\n"
    "After editing, write a small Markdown summary of the changes "
    "as report.md in the same directory."
)


async def run_e2e(
    *,
    narrator: Narrator,
    with_voice: bool,
    keep_project: bool,
    skip_cursor_open: bool,
    project_root: str | None,
    instruction: str,
) -> int:
    paged: list[RegistryEvent] = []

    async def narrator_pager(evt: RegistryEvent) -> None:
        """Driver-owned pager: narrates each interesting event to Discord.

        Subscribes to the unified `CursorAgentRegistry`'s emit callback
        so this matches production semantics. Only used when we started
        our own observer (with_voice=False mode); when with_voice=True
        the bot's running registry handles these events.
        """
        paged.append(evt)
        agent = evt.agent
        await narrator.say(
            f"Cursor just told me: {evt.reason} "
            f"(severity={evt.severity}, kind={evt.kind}, source={agent.source})",
            is_step=False,
        )
        if agent.last_assistant_text:
            preview = agent.last_assistant_text[:140].replace("\n", " ")
            print(
                f"     last assistant text: {preview!r}",
                flush=True,
            )

    tools_module.init_tools(cursor_bridge=CursorBridge())

    if project_root is None:
        project_root = tempfile.mkdtemp(prefix="aria_e2e_")
    proj_name = os.path.basename(project_root.rstrip("/")) or "aria_e2e"
    tools_module.PROJECT_REGISTRY[proj_name] = project_root
    print(f"\nregistered project: {proj_name} -> {project_root}", flush=True)

    obs: CursorExternalObserver | None = None
    if not with_voice:
        cursor_registry.set_project_aliases(dict(tools_module.PROJECT_REGISTRY))
        cursor_registry.set_emit_callback(narrator_pager)
        obs = CursorExternalObserver(
            registry_provider=lambda: dict(tools_module.PROJECT_REGISTRY),
            registry_writer=cursor_registry.register_from_hook,
        )
        try:
            await obs.start()
        except OSError as exc:
            print(f"FATAL: could not start observer: {exc}", flush=True)
            print(
                f"If port {config.cursor_event_port} is already in use, the bot is "
                "probably running — re-run with --with-voice (the bot's observer "
                "will handle the events).",
                flush=True,
            )
            return 1
        print(f"observer running on {obs.url}", flush=True)
    else:
        print(
            "  --with-voice ON: relying on the bot's running observer at "
            f"{config.cursor_event_host}:{config.cursor_event_port}. "
            "Observer pages will appear in #ucs-alerts (bot's pager); "
            "driver steps will appear in the configured text channel.",
            flush=True,
        )

    exit_code = 0
    try:
        # ---- Opening narration --------------------------------------------------
        if with_voice:
            await narrator.say(
                f"Starting an end-to-end Cursor narration test. The bot's already "
                f"running and I'm in your voice channel, so I'll speak each step "
                f"out loud while it happens. Project name is `{proj_name}`.",
            )
        else:
            await narrator.say(
                f"Starting an end-to-end Cursor narration test. My ear's on "
                f"`{obs.url if obs else 'no observer'}` "
                f"so I'll catch any Cursor lifecycle event. Project name is "
                f"`{proj_name}`.",
            )

        # ---- Project setup ------------------------------------------------------
        if not skip_cursor_open:
            await narrator.say(
                f"Creating a tiny dummy project at `{project_root}` — just a "
                "one-function calculator skeleton."
            )
            make_skeleton_project(project_root)

            await narrator.say(
                "Opening that project in a new Cursor window now."
            )
            proc = await asyncio.create_subprocess_exec(
                "open", "-a", "Cursor", project_root
            )
            await proc.wait()
            print(f"  open exit={proc.returncode}", flush=True)
            await asyncio.sleep(6.0)
        else:
            await narrator.say(
                f"Skipping the Cursor open step — using the project already at "
                f"`{project_root}`."
            )
            if not os.path.isdir(project_root):
                print(
                    f"  WARN: {project_root} does not exist on disk; "
                    "read tools will return no turns until Cursor creates it.",
                    flush=True,
                )

        # ---- Verify Cursor sees the window --------------------------------------
        await narrator.say(
            "Checking I can actually see the window — calling "
            "`list_cursor_windows`."
        )
        r = await tools_module.handle_tool_call("list_cursor_windows", {})
        parsed = json.loads(r)
        windows = parsed.get("windows", [])
        print(
            f"  list_cursor_windows: {len(windows)} window(s) visible",
            flush=True,
        )
        target_window = next(
            (w for w in windows if proj_name in (w.get("title") or "")), None
        )
        if target_window is None:
            await narrator.say(
                f"I don't yet see a window titled with `{proj_name}` — "
                "I'll proceed with substring matching, but heads up.",
                is_step=False,
            )

        # ---- Focus + read existing state ----------------------------------------
        await narrator.say(
            f"Bringing the `{proj_name}` window to the front."
        )
        r = await tools_module.handle_tool_call(
            "focus_cursor_window", {"project": proj_name}
        )
        print(f"  focus result: {r}", flush=True)
        await asyncio.sleep(1.5)

        await narrator.say(
            "Reading the current state of the chat sidebar to know where we are."
        )
        r = await tools_module.handle_tool_call(
            "read_cursor_window", {"project": proj_name, "n_turns": 3}
        )
        parsed = json.loads(r)
        print(
            f"  read_cursor_window: project={parsed.get('project')} "
            f"cwd={parsed.get('cwd')} turns_returned={parsed.get('turns_returned')}",
            flush=True,
        )

        # ---- Submit the plan ----------------------------------------------------
        await narrator.say(
            "Submitting the plan now — asking Cursor to extend `calculator.py` "
            "with `subtract` and `multiply`, add type hints and docstrings, and "
            "write a `report.md` summary."
        )
        print(f"  --- BEGIN INSTRUCTION ---\n{instruction}\n  --- END INSTRUCTION ---", flush=True)
        r = await tools_module.handle_tool_call(
            "send_to_cursor_chat",
            {"project": proj_name, "message": instruction},
        )
        print(f"  send_to_cursor_chat result: {r}", flush=True)
        await asyncio.sleep(1.5)

        # ---- Watch the send land ------------------------------------------------
        await narrator.say(
            "Plan submitted. Watching the transcript to confirm it actually "
            "landed in Cursor."
        )
        landed_via_transcript = False
        for poll in range(8):
            await asyncio.sleep(5)
            r = await tools_module.handle_tool_call(
                "read_cursor_window", {"project": proj_name, "n_turns": 4}
            )
            p = json.loads(r)
            turns = p.get("turns") or []
            if turns:
                latest = turns[-1]
                role = latest.get("role")
                has_tool = latest.get("has_tool_use")
                text = (latest.get("text") or "")[:140]
                print(
                    f"  poll {poll+1}/8 t={5*(poll+1)}s: turns={len(turns)} "
                    f"latest=[{role} tool_use={has_tool}] {text!r}",
                    flush=True,
                )
                last_user_turn = [t for t in turns if t.get("role") == "user"]
                if last_user_turn:
                    landed_via_transcript = True
                    await narrator.say(
                        "The plan landed — Cursor's working on it now.",
                        is_step=False,
                    )
                    break
            else:
                print(f"  poll {poll+1}/8 t={5*(poll+1)}s: still 0 turns", flush=True)

        if not landed_via_transcript:
            await narrator.say(
                "The plan didn't show up in the transcript after 40 seconds. "
                "I'd retry once in production; for the test I'll keep watching.",
                is_step=False,
            )

        # ---- Monitor while Cursor builds ----------------------------------------
        await narrator.say(
            "Monitoring hooks and the project directory for the next minute "
            "while Cursor builds."
        )
        deadline = time.time() + 60
        last_seen = 0
        last_files: set[str] = (
            set(os.listdir(project_root)) if os.path.isdir(project_root) else set()
        )
        while time.time() < deadline:
            await asyncio.sleep(5)
            now_files = (
                set(os.listdir(project_root)) if os.path.isdir(project_root) else set()
            )
            new_files = now_files - last_files
            if new_files:
                await narrator.say(
                    f"New files in the project: {sorted(new_files)}.",
                    is_step=False,
                )
                last_files = now_files
            if len(paged) > last_seen:
                print(
                    f"  + {len(paged) - last_seen} new event(s) "
                    f"(total paged so far: {len(paged)})",
                    flush=True,
                )
                last_seen = len(paged)
            stop_events = [e for e in paged if e.hook_type == "stop"]
            if stop_events:
                await narrator.say(
                    "Cursor just hit `stop` — its agent finished a turn. "
                    "Wrapping up the monitor loop.",
                    is_step=False,
                )
                break

        # ---- Read what happened -------------------------------------------------
        await narrator.say(
            "Reading the transcript again to see what Cursor actually did."
        )
        r = await tools_module.handle_tool_call(
            "read_cursor_window", {"project": proj_name, "n_turns": 6}
        )
        parsed = json.loads(r)
        print(
            f"  turns_returned={parsed.get('turns_returned')} "
            f"recent_plans={len(parsed.get('recent_plans') or [])}",
            flush=True,
        )
        for t in parsed.get("turns", [])[-5:]:
            text = (t.get("text") or "").replace("\n", " ")
            print(
                f"    [{t.get('role')}] {text[:160]!r} tool_use={t.get('has_tool_use')}",
                flush=True,
            )

        # ---- Verify files on disk -----------------------------------------------
        await narrator.say(
            "Checking the files on disk to confirm Cursor's edits landed."
        )
        if os.path.isdir(project_root):
            files = sorted(os.listdir(project_root))
            print(f"  files in project: {files}", flush=True)
            calc = os.path.join(project_root, "calculator.py")
            if os.path.exists(calc):
                with open(calc) as f:
                    body = f.read()
                has_subtract = "def subtract" in body
                has_multiply = "def multiply" in body
                await narrator.say(
                    f"`calculator.py`: subtract={has_subtract} multiply={has_multiply} "
                    f"({len(body)} bytes total).",
                    is_step=False,
                )
            report = os.path.join(project_root, "report.md")
            if os.path.exists(report):
                with open(report) as f:
                    body = f.read()
                await narrator.say(
                    f"`report.md` exists ({len(body)} bytes). First 300 chars: "
                    f"`{body[:300].replace(chr(10), ' ')}`",
                    is_step=False,
                )
            else:
                await narrator.say(
                    "`report.md` was NOT created. Cursor didn't fully complete the plan.",
                    is_step=False,
                )

        # ---- Summary ------------------------------------------------------------
        page_summary = ""
        if obs is not None:
            page_summary = (
                f" Observer stats: {obs.stats.get('events_seen', 0)} events seen, "
                f"{obs.stats.get('events_paged', 0)} paged."
            )
        await narrator.say(
            f"That's the end of the narration test.{page_summary} "
            f"Project root: `{project_root}`."
        )

        return exit_code

    finally:
        if obs is not None:
            try:
                await obs.stop()
            except Exception as exc:
                print(f"  WARN: observer stop raised: {exc}", flush=True)
        if not keep_project and project_root and project_root.startswith("/tmp/aria_e2e_"):
            cleanup_project(project_root)
            print(f"\nremoved {project_root}", flush=True)
        elif project_root:
            print(
                f"\n(project kept at {project_root}; close the Cursor window when done)",
                flush=True,
            )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _validate_discord_config() -> tuple[str, str]:
    """Return (bot_token, text_channel_id) or exit loud."""
    token = config.discord_bot_token
    channel_id = config.discord_text_channel_id
    missing: list[str] = []
    if not token:
        missing.append("DISCORD_APP_BOT_TOKEN")
    if not channel_id:
        missing.append("DISCORD_TEXT_CHANNEL_ID")
    if missing:
        print(
            f"FATAL: missing .env keys: {missing}. Set them in "
            f"{os.path.join(REPO_ROOT, '.env')} and re-run.",
            flush=True,
        )
        sys.exit(2)
    return token, channel_id


def _aria_say_url() -> str:
    host = config.cursor_event_host or "127.0.0.1"
    port = config.cursor_event_port or 8731
    return f"http://{host}:{port}/aria_say"


async def _main_async(args: argparse.Namespace) -> int:
    bot_token, text_channel_id = _validate_discord_config()
    aria_say_url = _aria_say_url()

    async with Narrator(
        bot_token=bot_token,
        text_channel_id=text_channel_id,
        with_voice=args.with_voice,
        aria_say_url=aria_say_url,
    ) as narrator:
        return await run_e2e(
            narrator=narrator,
            with_voice=args.with_voice,
            keep_project=args.keep_project,
            skip_cursor_open=args.skip_cursor_open,
            project_root=args.project_root,
            instruction=args.instruction or DEFAULT_INSTRUCTION,
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--with-voice",
        action="store_true",
        help=(
            "Also POST each narration line to the bot's /aria_say endpoint so "
            "Aria speaks it aloud. Requires the bot to be running and Gemini "
            "to be connected (i.e. you joined the voice channel)."
        ),
    )
    ap.add_argument(
        "--keep-project",
        action="store_true",
        help="Don't rm the /tmp/aria_e2e_… project after the test.",
    )
    ap.add_argument(
        "--skip-cursor-open",
        action="store_true",
        help="Don't `open -a Cursor`; assume the project window is already open.",
    )
    ap.add_argument(
        "--project-root",
        help="Use this project root instead of creating one under /tmp.",
    )
    ap.add_argument(
        "--instruction",
        help="Custom instruction to paste into the Cursor chat (default extends "
             "calculator.py with subtract/multiply and writes report.md).",
    )
    args = ap.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
